"""使用正式迁移后的隔离 PostgreSQL 验证事实事务，绝不连接运行库。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError
from tortoise import Tortoise

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemAssertionEvidence,
    DBMemAuditEvent,
    DBMemDomainOutbox,
    DBMemEventDelivery,
    DBMemIndexOutbox,
    DBMemLegacyMap,
    DBMemObservation,
    DBMemScope,
    DBMemScopeGrant,
    DBMemWriteRequest,
)
from nekro_agent.services.memory.contracts import FactProposal, FactStatus, MemoryError, RequestContext, SourceRef
from nekro_agent.services.memory.service import MemoryService, message_revision

DB_URL = os.environ.get("MEMORY_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")
ALICE = "onebot_v11:alice"


@pytest_asyncio.fixture(autouse=True)
async def isolated_db() -> AsyncIterator[None]:
    assert DB_URL == "postgres://memory_test:memory_test@127.0.0.1:5432/memory_test"
    await Tortoise.init(db_url=DB_URL, modules={"models": ["nekro_agent.models"]}, use_tz=True, timezone="UTC")
    for model in (
        DBMemAssertionEvidence, DBMemWriteRequest, DBMemEventDelivery, DBMemLegacyMap,
        DBMemIndexOutbox, DBMemDomainOutbox, DBMemAuditEvent, DBMemScopeGrant,
        DBMemAssertion, DBMemObservation, DBMemScope, DBChatMessage, DBChatChannel,
    ):
        await model.all().delete()
    for chat_key, workspace_id in (("private_alice", 1), ("group_a", 1), ("other_workspace", 2)):
        await DBChatChannel.create(
            adapter_key="onebot_v11", channel_id=chat_key, chat_key=chat_key,
            workspace_id=workspace_id, data="{}",
        )
    yield
    await Tortoise.close_connections()


def context(chat_key: str = "private_alice", **kwargs: object) -> RequestContext:
    return RequestContext(workspace_id=1, agent_id="test-agent", actor_key=ALICE, chat_key=chat_key, **kwargs)


async def proposal(
    value: str = "tea", *, confidence: float = 0.8, subject: str = ALICE,
    chat_key: str = "private_alice", predicate: str = "drink", valid_from: datetime | None = None,
) -> FactProposal:
    message = await DBChatMessage.create(
        sender_id="alice", sender_name="Alice", sender_nickname="Alice", is_tome=1,
        is_recalled=False, adapter_key="onebot_v11", message_id=f"synthetic-{time.time_ns()}",
        chat_key=chat_key, chat_type="private", platform_userid="alice", content_text=f"I prefer {value}",
        content_data="[]", raw_cq_code="", ext_data="{}", send_timestamp=int(time.time()),
    )
    return FactProposal(
        subject_key=subject, predicate=predicate, value=value, confidence=confidence,
        source=SourceRef(kind="message", id=str(message.id), revision=message_revision(message)),
        valid_from=valid_from,
    )


async def test_fact_correction_keeps_history_and_own_confidence() -> None:
    service = MemoryService()
    first = await service.propose_fact(context(), await proposal("tea", confidence=0.99), idempotency_key="first")
    second = await service.propose_fact(context(), await proposal("coffee", confidence=0.3), idempotency_key="second")
    bundle = await service.recall(context())
    assert [(item.value, item.confidence) for item in bundle.items] == [("coffee", 0.3)]
    assert second.old_assertion_id == first.assertion_id
    old = await DBMemAssertion.get(id=first.assertion_id)
    assert old.status == FactStatus.SUPERSEDED and old.valid_to is not None
    assert await DBMemIndexOutbox.all().count() == 3
    assert await DBMemDomainOutbox.all().count() == 3


async def test_late_idempotent_retry_cannot_restore_old_value() -> None:
    service = MemoryService()
    first = await proposal("tea")
    result = await service.propose_fact(context(), first, idempotency_key="first")
    await service.propose_fact(context(), await proposal("coffee"), idempotency_key="second")
    retry = await service.propose_fact(context(), first, idempotency_key="first")
    assert retry.assertion_id == result.assertion_id and retry.status == FactStatus.SUPERSEDED
    assert [item.value for item in (await service.recall(context())).items] == ["coffee"]
    assert await DBMemAssertion.all().count() == 2


async def test_idempotency_key_reuse_with_different_payload_rejected() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="same")
    with pytest.raises(MemoryError, match="IDEMPOTENCY_CONFLICT"):
        await service.propose_fact(context(), await proposal("coffee"), idempotency_key="same")
    assert await DBMemAssertion.all().count() == 1


async def test_concurrent_updates_have_one_current_version() -> None:
    service = MemoryService()
    await service.resolve_scope(context())
    proposals = [await proposal(str(i)) for i in range(8)]
    await asyncio.gather(*[
        service.propose_fact(context(), value, idempotency_key=f"concurrent-{i}")
        for i, value in enumerate(proposals)
    ])
    assert await DBMemAssertion.filter(status=FactStatus.ACTIVE).count() == 1
    assert await DBMemAssertion.all().count() == 8
    assert sorted(await DBMemAssertion.all().values_list("semantic_version", flat=True)) == list(range(1, 9))


async def test_concurrent_identical_requests_only_commit_once() -> None:
    service = MemoryService()
    value = await proposal()
    results = await asyncio.gather(*[service.propose_fact(context(), value, idempotency_key="same") for _ in range(5)])
    assert len({r.assertion_id for r in results}) == 1
    assert await DBMemAssertion.all().count() == 1
    assert await DBMemDomainOutbox.all().count() == 1


async def test_audit_failure_rolls_back_entire_fact_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MemoryService()
    monkeypatch.setattr(DBMemAuditEvent, "create", AsyncMock(side_effect=RuntimeError("synthetic-db-failure")))
    with pytest.raises(RuntimeError, match="synthetic-db-failure"):
        await service.propose_fact(context(), await proposal(), idempotency_key="atomic")
    assert await DBMemAssertion.all().count() == 0
    assert await DBMemObservation.all().count() == 0
    assert await DBMemWriteRequest.all().count() == 0
    assert await DBMemIndexOutbox.all().count() == 0


async def test_source_revision_and_origin_are_verified() -> None:
    service = MemoryService()
    value = await proposal()
    for source in (value.source.model_copy(update={"revision": "invalid"}), value.source):
        request_context = context("group_a") if source == value.source else context()
        with pytest.raises(MemoryError, match="SOURCE_INVALID"):
            await service.propose_fact(request_context, value.model_copy(update={"source": source}), idempotency_key="bad")


async def test_source_edit_or_recall_blocks_existing_fact_immediately() -> None:
    service = MemoryService()
    value = await proposal()
    await service.propose_fact(context(), value, idempotency_key="source")
    bundle = await service.recall(context())
    await DBChatMessage.filter(id=int(value.source.id)).update(content_text="edited source")
    assert not (await service.recall(context())).items
    with pytest.raises(MemoryError, match="SOURCE_INVALID"):
        await service.validate_bundle(context(), bundle)


async def test_cross_chat_and_agent_are_isolated() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="private")
    assert not (await service.recall(context("group_a"))).items
    assert not (await service.recall(context().model_copy(update={"agent_id": "other-agent"}))).items
    with pytest.raises(MemoryError, match="SCOPE_UNRESOLVED"):
        await service.recall(context("other_workspace"))
    with pytest.raises(MemoryError, match="SCOPE_UNRESOLVED"):
        await service.recall(context("unbound"))


async def test_explicit_share_and_revoke_invalidates_old_bundle() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="private")
    admin = context(is_admin=True, purpose="admin_review")
    grant = await service.grant_scope(admin, audience_chat_key="group_a")
    bundle = await service.recall(context("group_a"))
    assert [item.value for item in bundle.items] == ["tea"]
    await service.revoke_grant(admin, grant)
    assert not (await service.recall(context("group_a"))).items
    with pytest.raises(MemoryError, match="POLICY_CHANGED"):
        await service.validate_bundle(context("group_a"), bundle)


async def test_grant_expiry_is_checked_without_background_worker() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="private")
    grant = await service.grant_scope(context(is_admin=True, purpose="admin_review"), audience_chat_key="group_a")
    await DBMemScopeGrant.filter(id=grant).update(valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert not (await service.recall(context("group_a"))).items


async def test_forget_blocks_all_versions_and_late_source_replay() -> None:
    service = MemoryService()
    first = await proposal()
    result = await service.propose_fact(context(), first, idempotency_key="first")
    await service.propose_fact(context(), await proposal("coffee"), idempotency_key="second")
    receipt = await service.forget(context(), result.assertion_id)
    assert receipt.affected_assertions == 2 and receipt.logical_block_committed
    assert not (await service.recall(context())).items
    assert (await service.propose_fact(context(), first, idempotency_key="first")).status == FactStatus.DELETED
    with pytest.raises(MemoryError, match="SOURCE_INVALID"):
        await service.propose_fact(context(), first, idempotency_key="late-new-job")
    assert (await service.forget(context(), result.assertion_id)).affected_assertions == 0


async def test_deleted_fact_can_be_explicitly_saved_from_new_source() -> None:
    service = MemoryService()
    first = await service.propose_fact(context(), await proposal(), idempotency_key="first")
    await service.forget(context(), first.assertion_id)
    await service.propose_fact(context(), await proposal("tea"), idempotency_key="new-source")
    assert [item.value for item in (await service.recall(context())).items] == ["tea"]


async def test_manual_admin_fact_cannot_be_silently_overwritten() -> None:
    service = MemoryService()
    manual = FactProposal(
        subject_key=ALICE, predicate="drink", value="tea", confidence=0.5,
        source=SourceRef(kind="admin_edit", id="manual-1", revision="1"),
    )
    await service.propose_fact(context(is_admin=True), manual, idempotency_key="manual")
    result = await service.propose_fact(context(), await proposal("coffee", confidence=1), idempotency_key="auto")
    assert result.status == FactStatus.PENDING
    assert [item.value for item in (await service.recall(context())).items] == ["tea"]


async def test_third_party_statement_requires_review() -> None:
    result = await MemoryService().propose_fact(
        context(), await proposal(subject="onebot_v11:bob"), idempotency_key="third-party",
    )
    assert result.status == FactStatus.PENDING
    assert not (await MemoryService().recall(context())).items


async def test_multivalued_attribute_preserves_other_values() -> None:
    service = MemoryService(multivalued_predicates=frozenset({"interest"}))
    for i, value in enumerate(("music", "books", "music")):
        await service.propose_fact(context(), await proposal(value, predicate="interest"), idempotency_key=str(i))
    assert sorted(item.value for item in (await service.recall(context())).items) == ["books", "music"]
    assert await DBMemAssertion.all().count() == 2
    assert await DBMemAssertionEvidence.all().count() == 3


async def test_future_and_stale_effective_dates_do_not_replace_current() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="current")
    now = datetime.now(timezone.utc)
    for i, offset in enumerate((-24, 24)):
        result = await service.propose_fact(
            context(), await proposal(str(i), valid_from=now + timedelta(hours=offset)), idempotency_key=str(i),
        )
        assert result.status == FactStatus.PENDING
    assert [item.value for item in (await service.recall(context())).items] == ["tea"]


async def test_stale_expected_version_is_rejected() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="current")
    with pytest.raises(MemoryError, match="VERSION_CONFLICT"):
        await service.propose_fact(context(), await proposal("coffee"), idempotency_key="stale", expected_row_revision=99)


@pytest.mark.parametrize("predicate", ["favorability", "relationship_score", "relationship_stage", "agent_persona", "permissions"])
async def test_relationship_and_config_domains_are_not_fact_fields(predicate: str) -> None:
    with pytest.raises(MemoryError, match="RESERVED_DOMAIN"):
        await MemoryService().propose_fact(context(), await proposal(predicate=predicate), idempotency_key="reserved")


@pytest.mark.parametrize("confidence", [-1, 2, float("nan"), float("inf")])
async def test_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        await proposal(confidence=confidence)


async def test_untrusted_admin_actions_are_rejected() -> None:
    service = MemoryService()
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.grant_scope(context(), audience_chat_key="group_a")
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.recall(context(purpose="admin_review"))


async def test_role_check_protects_other_users_sources_and_facts() -> None:
    service = MemoryService()
    value = await proposal()
    bob = context().model_copy(update={"actor_key": "onebot_v11:bob"})
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.propose_fact(bob, value, idempotency_key="stolen-source")
    result = await service.propose_fact(context(), value, idempotency_key="own-source")
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.forget(bob, result.assertion_id)


async def test_correction_invalidates_cached_bundle_without_waiting_for_index() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="old")
    bundle = await service.recall(context())
    await service.propose_fact(context(), await proposal("coffee"), idempotency_key="new")
    with pytest.raises(MemoryError, match="POLICY_CHANGED"):
        await service.validate_bundle(context(), bundle)
    assert await DBMemIndexOutbox.filter(state="pending").count() == 3


@pytest.mark.parametrize("value", ["123", "true", "null", '{"x":1}', 123, True, None, {"x": 1}, ["a", 2]])
async def test_json_scalar_types_are_preserved(value: object) -> None:
    service = MemoryService()
    item = (await proposal()).model_copy(update={"value": value})
    await service.propose_fact(context(), item, idempotency_key="typed")
    actual = (await service.recall(context())).items[0].value
    assert type(actual) is type(value) and actual == value
