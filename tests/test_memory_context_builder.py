"""验证统一装配不会截断事实、扩大关系权限或重复召回。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from test_memory_fact_service import DB_URL, context, isolated_db, proposal

from nekro_agent.services.agent.memory_context import ContextBudget, MemoryContextBuilder, RelationshipFragment
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


def budget(memory_limit: int = 1000, model_limit: int = 4096) -> ContextBudget:
    return ContextBudget(
        model_limit=model_limit, reserved_output=256, tool_reserve=256,
        safety_margin=64, prompt_overhead=64, memory_limit=memory_limit,
    )


async def test_context_uses_one_recall_and_preserves_complete_fact() -> None:
    service = MemoryService()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="fact")
    service.recall = AsyncMock(wraps=service.recall)
    builder = MemoryContextBuilder(service)
    bundle = await builder.build(context(), budget(), persona="只读人格", recent_history=("recent", "recent"))
    assert service.recall.await_count == 1
    assert bundle.fact_ids == [result.assertion_id]
    # 近期窗口只做预算接纳，不进记忆块正文
    assert bundle.history == ["recent"] and "recent" not in bundle.text and '"tea"' in bundle.text
    assert bundle.layer_units["facts"] > 0
    assert bundle.used_units <= budget().available
    assert bundle.budget_method == "utf8_bytes_upper_bound"


async def test_over_budget_fact_is_dropped_as_a_whole() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal("long" * 1000), idempotency_key="long")
    bundle = await MemoryContextBuilder(service).build(context(), budget(memory_limit=100))
    assert not bundle.fact_ids and "long" not in bundle.text
    assert any(reason.startswith("fact:") for reason in bundle.dropped)


async def test_required_persona_is_not_silently_truncated() -> None:
    with pytest.raises(MemoryError, match="CONTEXT_REQUIRED_BLOCK_TOO_LARGE"):
        await MemoryContextBuilder(MemoryService()).build(context(), budget(model_limit=800), persona="规则" * 1000)


async def test_relationship_without_validator_is_never_used() -> None:
    provider = AsyncMock(return_value=RelationshipFragment(text="private relationship", relationship_revision=1))
    bundle = await MemoryContextBuilder(MemoryService(), relationship_provider=provider).build(context(), budget())
    assert "private relationship" not in bundle.text and provider.await_count == 0


async def test_unauthorized_relationship_falls_back_without_affecting_facts() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="fact")
    provider = AsyncMock(return_value=RelationshipFragment(text="private relationship", relationship_revision=1))
    validator = AsyncMock(return_value=False)
    bundle = await MemoryContextBuilder(
        service, relationship_provider=provider, relationship_validator=validator,
    ).build(context(), budget())
    assert '"tea"' in bundle.text and "private relationship" not in bundle.text
    assert bundle.relationship is None and "relationship:unverified" in bundle.dropped


async def test_relationship_revocation_after_build_is_checked_before_send() -> None:
    service = MemoryService()
    validator = AsyncMock(return_value=True)
    builder = MemoryContextBuilder(
        service, relationship_provider=AsyncMock(return_value=RelationshipFragment(text="style", relationship_revision=1)),
        relationship_validator=validator,
    )
    bundle = await builder.build(context(), budget())
    assert bundle.relationship is not None
    validator.return_value = False
    with pytest.raises(MemoryError, match="RELATIONSHIP_POLICY_CHANGED"):
        await builder.validate(context(), bundle)


async def test_deleted_fact_cannot_use_previously_built_context() -> None:
    service = MemoryService()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="fact")
    builder = MemoryContextBuilder(service)
    bundle = await builder.build(context(), budget())
    await service.forget(context(), result.assertion_id)
    with pytest.raises(MemoryError, match="POLICY_CHANGED"):
        await builder.validate(context(), bundle)


async def test_explicit_budget_counter_can_be_supplied() -> None:
    builder = MemoryContextBuilder(MemoryService(), counter=len, budget_method="test_counter")
    bundle = await builder.build(context(), budget())
    assert bundle.used_units == len(bundle.text) and bundle.budget_method == "test_counter"


async def test_history_window_shares_one_available_budget() -> None:
    builder = MemoryContextBuilder(MemoryService(), counter=len, budget_method="test_counter")
    plan = budget(model_limit=1000)
    bundle = await builder.build(context(), plan, persona="人格", recent_history=("short", "x" * 400))
    assert bundle.used_units <= plan.available
    # 超出可用余量的行被整条丢弃，而不是截断
    assert bundle.history == ["short"] and "history" in bundle.dropped


async def test_registered_relationship_source_is_used_by_build_and_validate() -> None:
    from nekro_agent.services.agent.memory_context import (
        clear_relationship_source,
        register_relationship_source,
    )

    provider = AsyncMock(return_value=RelationshipFragment(text="registered relationship", relationship_revision=3))
    validator = AsyncMock(return_value=True)
    register_relationship_source(provider=provider, validator=validator, label="test")
    try:
        builder = MemoryContextBuilder(MemoryService())
        bundle = await builder.build(context(), budget())
        assert bundle.relationship is not None and "registered relationship" in bundle.text
        await builder.validate(context(), bundle)
        assert provider.await_count == 1 and validator.await_count >= 1
        validator.return_value = False
        with pytest.raises(MemoryError, match="RELATIONSHIP_POLICY_CHANGED"):
            await builder.validate(context(), bundle)
    finally:
        assert clear_relationship_source(label="test") is True


async def test_relationship_source_registry_rejects_non_callable() -> None:
    from nekro_agent.services.agent.memory_context import register_relationship_source

    with pytest.raises(MemoryError, match="RELATIONSHIP_SOURCE_INVALID"):
        register_relationship_source(provider=None, validator=object(), label="bad")


async def test_relationship_provider_failure_uses_neutral_context() -> None:
    builder = MemoryContextBuilder(
        MemoryService(), relationship_provider=AsyncMock(side_effect=RuntimeError("synthetic sqlite failure")),
        relationship_validator=AsyncMock(return_value=True),
    )
    bundle = await builder.build(context(), budget())
    assert bundle.relationship is None and "relationship:unavailable" in bundle.dropped
