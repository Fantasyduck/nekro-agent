"""验证工具入口把写入转成事实服务提交，并如实区分确定值与待确认。"""

from __future__ import annotations

import pytest
from test_memory_fact_service import ALICE, DB_URL, isolated_db, proposal

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_fact import DBMemAssertion
from nekro_agent.services.memory import tool_facade
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.ingest_worker import MemoryIngestQueue

__all__ = ["isolated_db"]
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


@pytest.fixture(autouse=True)
def facade_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_FACT_AGENT_ID", "test-agent")


async def activate(chat_key: str = "private_alice", mode: str = "service") -> None:
    from test_memory_fact_service import context

    await MemoryIngestQueue().set_mode(
        context(chat_key=chat_key, is_admin=True, purpose="admin_review"), mode, expected_writer_revision=1,
    )


async def test_tool_write_binds_latest_message_and_stays_active() -> None:
    await activate()
    await proposal("tea")
    result = await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE,
        subject_key=tool_facade.normalize_subject("onebot_v11", "alice"),
        predicate="drink", value="coffee",
    )
    assert result.status == "active" and result.action == "created"
    row = await DBMemAssertion.get(id=result.assertion_id)
    assert row.value_json["value"] == "coffee" and row.verification_status == "user_declared"


async def test_tool_write_about_another_subject_is_pending_not_success() -> None:
    await activate()
    await proposal("tea")
    result = await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE,
        subject_key=tool_facade.normalize_subject("onebot_v11", "bob"),
        predicate="drink", value="coffee",
    )
    assert result.status == "pending_review" and result.action == "pending_review"


async def test_notes_are_multivalued_and_keep_metadata() -> None:
    await activate()
    await proposal("tea")
    first = await tool_facade.remember_note(
        chat_key="private_alice", actor_key=ALICE,
        subject_key=tool_facade.normalize_subject("onebot_v11", "alice"),
        text="喜欢在雨天写代码", tags=["preference"], importance=0.8,
    )
    second = await tool_facade.remember_note(
        chat_key="private_alice", actor_key=ALICE,
        subject_key=tool_facade.normalize_subject("onebot_v11", "alice"),
        text="下周要交一份报告", tags=["commitment"], importance=0.6,
    )
    assert first.assertion_id != second.assertion_id
    rows = DBMemAssertion.filter(predicate=tool_facade.NOTE_PREDICATE, status="active")
    assert await rows.count() == 2
    payload = (await DBMemAssertion.get(id=first.assertion_id)).value_json["value"]
    assert payload["text"] == "喜欢在雨天写代码" and payload["tags"] == ["preference"]


async def test_note_title_and_origin_are_kept_and_idempotent_separately() -> None:
    await activate()
    await proposal("tea")
    subject = tool_facade.normalize_subject("onebot_v11", "alice")
    titled = await tool_facade.remember_note(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject,
        text="90 度水温", title="冲泡温度", origin="agent_tool",
    )
    plain = await tool_facade.remember_note(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject,
        text="90 度水温",
    )
    # 标题参与幂等键：同正文但不同标题必须是两条独立笔记，否则结构化笔记会互相覆盖。
    assert titled.assertion_id != plain.assertion_id
    titled_row = await DBMemAssertion.get(id=titled.assertion_id)
    payload = titled_row.value_json["value"]
    assert payload["title"] == "冲泡温度" and payload["origin"] == "agent_tool"
    plain_payload = (await DBMemAssertion.get(id=plain.assertion_id)).value_json["value"]
    assert "title" not in plain_payload and "origin" not in plain_payload


async def test_list_facts_filters_note_predicate() -> None:
    await activate()
    await proposal("tea")
    subject = tool_facade.normalize_subject("onebot_v11", "alice")
    await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject, predicate="drink", value="tea",
    )
    await tool_facade.remember_note(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject, text="笔记一", title="标题一",
    )
    page = await tool_facade.list_facts(
        chat_key="private_alice", actor_key=ALICE, predicate=tool_facade.NOTE_PREDICATE,
    )
    assert page.total == 1
    assert page.items[0].value["title"] == "标题一"


async def test_list_facts_reads_back_current_values() -> None:
    await activate()
    await proposal("tea")
    subject = tool_facade.normalize_subject("onebot_v11", "alice")
    await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject, predicate="drink", value="tea",
    )
    page = await tool_facade.list_facts(chat_key="private_alice", actor_key=ALICE, subject_key=subject)
    assert page.total == 1 and page.items[0].value == "tea"
    assert str(page.items[0].assertion_id) in page.policy_revisions or page.policy_revisions


async def test_forget_only_removes_own_fact() -> None:
    await activate()
    await proposal("tea")
    subject = tool_facade.normalize_subject("onebot_v11", "alice")
    result = await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE, subject_key=subject, predicate="drink", value="tea",
    )
    other = await tool_facade.remember_fact(
        chat_key="private_alice", actor_key=ALICE,
        subject_key=tool_facade.normalize_subject("onebot_v11", "bob"),
        predicate="drink", value="coffee",
    )
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await tool_facade.forget_fact(chat_key="private_alice", actor_key=ALICE, assertion_id=other.assertion_id)
    receipt = await tool_facade.forget_fact(
        chat_key="private_alice", actor_key=ALICE, assertion_id=result.assertion_id,
    )
    assert receipt.logical_block_committed and receipt.affected_assertions >= 1


async def test_legacy_short_id_mapping_roundtrip() -> None:
    from uuid import uuid4

    await activate()
    await proposal("tea")
    assertion_id = uuid4()
    await tool_facade.record_legacy_mapping(legacy_id="7", assertion_id=assertion_id)
    assert await tool_facade.resolve_legacy_mapping(legacy_id="7") == assertion_id
    assert await tool_facade.resolve_legacy_mapping(legacy_id="8") is None


async def test_tool_entry_refuses_channels_without_unified_pipeline() -> None:
    await proposal("tea")
    with pytest.raises(MemoryError, match="PIPELINE_DISABLED"):
        await tool_facade.remember_fact(
            chat_key="private_alice", actor_key=ALICE,
            subject_key=tool_facade.normalize_subject("onebot_v11", "alice"),
            predicate="drink", value="tea",
        )
