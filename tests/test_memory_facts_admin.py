"""管理面门面与路由映射的隔离数据库测试。"""

from __future__ import annotations

import pytest
from test_memory_fact_service import DB_URL, isolated_db, proposal
from test_memory_fact_service import context as base_context

from nekro_agent.models.db_mem_fact import DBMemAuditEvent, DBMemIndexOutbox, DBMemObservation
from nekro_agent.routers import memory_facts as memory_facts_router
from nekro_agent.schemas.errors import MemoryConflictError, OperationFailedError, PermissionDeniedError, ValidationError
from nekro_agent.services.memory.contracts import FactStatus, MemoryError
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]

pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


def admin_context(chat_key: str = "private_alice", **kwargs: object):
    return base_context(chat_key=chat_key, is_admin=True, purpose="admin_review", **kwargs)


async def test_list_facts_only_returns_current_version_with_policy_revision() -> None:
    service = MemoryService()
    first = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")
    second = await service.propose_fact(base_context(), await proposal("coffee"), idempotency_key="second")

    page = await service.list_facts(admin_context())

    assert page.total == 1
    assert [item.value for item in page.items] == ["coffee"]
    assert page.items[0].assertion_id == second.assertion_id
    assert page.items[0].status == FactStatus.ACTIVE
    assert page.items[0].source_refs, "管理面必须能核验来源，不能只给值"
    assert page.policy_revisions and all(isinstance(value, int) for value in page.policy_revisions.values())

    history = await service.list_facts(admin_context(), status=FactStatus.SUPERSEDED)
    assert [item.assertion_id for item in history.items] == [first.assertion_id]


async def test_get_fact_returns_full_version_chain_oldest_first() -> None:
    service = MemoryService()
    first = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")
    second = await service.propose_fact(base_context(), await proposal("coffee"), idempotency_key="second")

    detail = await service.get_fact(admin_context(), first.assertion_id)

    assert [item.semantic_version for item in detail.versions] == [1, 2]
    assert [item.assertion_id for item in detail.versions] == [first.assertion_id, second.assertion_id]
    assert detail.current.assertion_id == second.assertion_id
    assert detail.versions[-1].supersedes_id == first.assertion_id


async def test_admin_correction_creates_new_version_and_audits() -> None:
    service = MemoryService()
    original = await service.propose_fact(base_context(), await proposal("tea", confidence=0.9), idempotency_key="first")

    result = await service.correct_fact(
        admin_context(), original.assertion_id, value="oolong", confidence=0.95, reason="用户更正",
    )

    assert result.action == "corrected"
    assert result.old_assertion_id == original.assertion_id
    assert [item.value for item in (await service.recall(base_context())).items] == ["oolong"]
    detail = await service.get_fact(admin_context(), original.assertion_id)
    assert [item.value for item in detail.versions] == ["tea", "oolong"]
    assert detail.current.verification_status == "manual_admin"
    operations = await DBMemAuditEvent.all().values_list("operation", flat=True)
    assert "fact.admin_edited" in operations
    assert await DBMemObservation.filter(source_kind="admin_edit").count() == 1
    assert await DBMemIndexOutbox.all().count() == 3


async def test_admin_correction_rejects_stale_row_revision() -> None:
    service = MemoryService()
    original = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")
    await service.correct_fact(admin_context(), original.assertion_id, value="oolong")

    with pytest.raises(MemoryError, match="VERSION_CONFLICT"):
        await service.correct_fact(
            admin_context(), original.assertion_id, value="jasmine", expected_row_revision=1,
        )

    detail = await service.get_fact(admin_context(), original.assertion_id)
    assert detail.current.value == "oolong"


async def test_management_surface_requires_admin_review() -> None:
    service = MemoryService()
    original = await service.propose_fact(base_context(), await proposal(), idempotency_key="first")

    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.correct_fact(base_context(), original.assertion_id, value="coffee")
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.list_grants(base_context())
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.correct_fact(
            base_context(purpose="admin_review"), original.assertion_id, value="coffee",
        )


async def test_grants_are_listed_with_active_flag() -> None:
    service = MemoryService()
    grant_id = await service.grant_scope(admin_context(), audience_chat_key="group_a")

    records = await service.list_grants(admin_context())

    assert [record.grant_id for record in records] == [grant_id]
    assert records[0].audience_chat_key == "group_a"
    assert records[0].chat_key == "private_alice"
    assert records[0].active is True

    await service.revoke_grant(admin_context(), grant_id)
    revoked = await service.list_grants(admin_context())
    assert revoked[0].active is False and revoked[0].revoked_at is not None


def test_error_mapping_hides_existence_and_keeps_semantics() -> None:
    assert isinstance(memory_facts_router._to_app_error(MemoryError("SCOPE_DENIED")), PermissionDeniedError)
    assert isinstance(memory_facts_router._to_app_error(MemoryError("VERSION_CONFLICT")), MemoryConflictError)
    assert isinstance(memory_facts_router._to_app_error(MemoryError("SOURCE_INVALID")), MemoryConflictError)
    assert isinstance(memory_facts_router._to_app_error(MemoryError("LIMIT_INVALID")), ValidationError)
    assert isinstance(memory_facts_router._to_app_error(MemoryError("KIND_INVALID")), ValidationError)
    assert isinstance(memory_facts_router._to_app_error(MemoryError("SOMETHING_NEW")), OperationFailedError)


def test_status_filter_parsing() -> None:
    assert memory_facts_router._status_value("active") is FactStatus.ACTIVE
    assert memory_facts_router._status_value("all") is None
    with pytest.raises(ValidationError):
        memory_facts_router._status_value("unknown")


def test_admin_context_is_server_injected() -> None:
    context = memory_facts_router._admin_context(workspace_id=2, chat_key="group_a", username="root")
    assert context.is_admin is True
    assert context.purpose == "admin_review"
    assert context.workspace_id == 2
    assert context.actor_key == "web:root"
