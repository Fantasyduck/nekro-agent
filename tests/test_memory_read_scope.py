"""使用合成记录验证陈旧索引、图谱扩展与猜测 ID 无法跨频道泄露。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from qdrant_client import models as qdrant_models

from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import qdrant_manager, query_service, retriever
from nekro_agent.services.memory.read_scope import (
    MemoryReadScope,
    MemoryScopeError,
    require_read_scope,
    require_visible_memory,
)

CHANNEL = "onebot_v11-group_test"
PRIVATE = "onebot_v11-private_other"


def _matches(row: SimpleNamespace, filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        field, _, lookup = key.partition("__")
        value = getattr(row, field, None)
        if lookup == "in":
            passed = value in expected
        elif lookup == "icontains":
            passed = str(expected).lower() in str(value).lower()
        elif lookup == "gte":
            passed = value is not None and value >= expected
        elif lookup == "lte":
            passed = value is not None and value <= expected
        else:
            passed = value == expected
        if not passed:
            return False
    return True


def _matches_q(row: SimpleNamespace, condition: Any) -> bool:
    terms = [_matches(row, condition.filters)] if condition.filters else []
    terms.extend(_matches_q(row, child) for child in condition.children)
    return any(terms) if condition.join_type == "OR" else all(terms)


class _Query:
    """小型异步查询替身，仅隔离数据库，不替换被测的读取逻辑。"""

    def __init__(self, rows: list[SimpleNamespace]):
        self.rows = rows

    def filter(self, *conditions: Any, **filters: Any) -> _Query:
        return _Query([
            row for row in self.rows
            if _matches(row, filters) and all(_matches_q(row, condition) for condition in conditions)
        ])

    def order_by(self, *fields: str) -> _Query:
        rows = list(self.rows)
        for field in reversed(fields):
            rows.sort(key=lambda row: getattr(row, field.lstrip("-")), reverse=field.startswith("-"))
        return _Query(rows)

    def limit(self, limit: int) -> _Query:
        return _Query(self.rows[:limit])

    async def all(self) -> list[SimpleNamespace]:
        return self.rows

    async def values_list(self, field: str, flat: bool = False) -> list[Any]:
        return [getattr(row, field) if flat else (getattr(row, field),) for row in self.rows]

    def __await__(self):
        return self.all().__await__()


def _install_rows(monkeypatch: pytest.MonkeyPatch, model: Any, rows: list[SimpleNamespace]) -> None:
    monkeypatch.setattr(model, "filter", lambda *conditions, **filters: _Query(rows).filter(*conditions, **filters))

    async def get_or_none(**filters: Any) -> SimpleNamespace | None:
        return next(iter(_Query(rows).filter(**filters).rows), None)

    monkeypatch.setattr(model, "get_or_none", get_or_none)


@pytest.fixture
def memory_rows(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    now = datetime.now(timezone.utc)

    def paragraph(pid: int, origin: str | None, workspace: int = 1, inactive: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            id=pid, workspace_id=workspace, origin_chat_key=origin, is_inactive=inactive,
            content=f"paragraph-{pid}", summary=f"summary-{pid}",
            cognitive_type=CognitiveType.EPISODIC, knowledge_type=KnowledgeType.FACT,
            event_time=now, episode_id=20 if pid == 1 else None,
            compute_effective_weight=lambda _timestamp: 1.0, memory_source="na",
            origin_kind=SimpleNamespace(value="chat"), origin_ref=None,
            anchor_msg_id=None, anchor_msg_id_start=None, anchor_msg_id_end=None,
            anchor_timestamp_start=None, anchor_timestamp_end=None,
        )

    paragraphs = [
        paragraph(1, CHANNEL), paragraph(2, PRIVATE), paragraph(3, None),
        paragraph(4, CHANNEL, inactive=True), paragraph(5, CHANNEL, workspace=2),
    ]
    entities = [SimpleNamespace(id=i, workspace_id=1, canonical_name=f"test-{i}", name=f"test-{i}", is_inactive=False) for i in (1, 2)]
    relations = [
        SimpleNamespace(
            id=rid, workspace_id=1, paragraph_id=pid, subject_entity_id=1, object_entity_id=2,
            predicate="test", is_inactive=False, update_time=now, cognitive_type="episodic",
            memory_source="na", compute_effective_weight=lambda _timestamp: 1.0,
        )
        for rid, pid in [(10, 1), (11, 2), (12, None)]
    ]
    episodes = [
        SimpleNamespace(
            id=eid, workspace_id=1, origin_chat_key=origin, paragraph_ids=ids,
            title="test", narrative_summary=f"episode-{eid}", is_inactive=False,
            base_weight=1.0, time_start=now, time_end=now, participant_entity_ids=[],
        )
        for eid, origin, ids in [(20, CHANNEL, [1]), (21, PRIVATE, [2]), (22, CHANNEL, [1, 2]), (23, CHANNEL, [])]
    ]
    for model, rows in [(DBMemParagraph, paragraphs), (DBMemEntity, entities), (DBMemRelation, relations), (DBMemEpisode, episodes)]:
        _install_rows(monkeypatch, model, rows)
    monkeypatch.setattr(retriever, "is_memory_system_enabled", lambda: True)
    embed = AsyncMock(return_value=[0.1, 0.2])
    # 即使索引忽略过滤或返回陈旧命中，SQL 复核也必须排除跨频道和失活段落。
    search = AsyncMock(return_value=[{"id": row.id, "score": 0.9} for row in paragraphs])
    access = AsyncMock()
    monkeypatch.setattr(retriever, "embed_text", embed)
    monkeypatch.setattr(retriever.memory_qdrant_manager, "search", search)
    monkeypatch.setattr(retriever.MemoryRetriever, "_record_access", access)
    return SimpleNamespace(paragraphs=paragraphs, search=search, embed=embed, access=access)


@pytest.mark.parametrize("workspace_id", [0, -1, True, "1"])
def test_invalid_workspace_is_rejected(workspace_id: Any) -> None:
    with pytest.raises(MemoryScopeError):
        MemoryReadScope.for_channel(workspace_id, CHANNEL)


@pytest.mark.parametrize("chat_key", ["", " ", " group ", None, True])
def test_invalid_channel_is_rejected(chat_key: Any) -> None:
    with pytest.raises(MemoryScopeError):
        MemoryReadScope.for_channel(1, chat_key)


def test_scope_is_immutable_and_does_not_authorize_private_origin() -> None:
    scope = MemoryReadScope.for_channel(1, CHANNEL)
    assert scope.allows_origin(CHANNEL)
    assert not scope.allows_origin(PRIVATE)
    assert not scope.allows_origin(None)
    with pytest.raises(FrozenInstanceError):
        scope.chat_key = PRIVATE


def test_missing_or_cross_workspace_scope_is_rejected() -> None:
    with pytest.raises(MemoryScopeError):
        retriever.MemoryRetriever(1)
    with pytest.raises(MemoryScopeError):
        require_read_scope(2, MemoryReadScope.for_channel(1, CHANNEL))


async def test_channel_recall_blocks_stale_vectors_relations_and_mixed_episodes(memory_rows: SimpleNamespace) -> None:
    reader = retriever.MemoryRetriever(1, scope=MemoryReadScope.for_channel(1, CHANNEL))
    result = await reader.retrieve("test", limit=20)
    assert {(item.source_type, item.target_id) for item in result.memories} == {
        ("paragraph", 1), ("relation", 10), ("episode", 20),
    }
    assert all(item.origin_chat_key == CHANNEL for item in result.memories)
    assert memory_rows.search.await_args.kwargs["paragraph_ids"] == [1]
    memory_rows.access.assert_awaited_once()
    assert memory_rows.access.await_args.args[0] == result.memories


async def test_no_authorized_paragraphs_skips_embedding_and_index(memory_rows: SimpleNamespace) -> None:
    reader = retriever.MemoryRetriever(1, scope=MemoryReadScope.for_channel(1, "group_empty"))
    assert (await reader.retrieve("test")).memories == []
    memory_rows.embed.assert_not_awaited()
    memory_rows.search.assert_not_awaited()


async def test_channel_cannot_request_inactive_records(memory_rows: SimpleNamespace) -> None:
    reader = retriever.MemoryRetriever(1, scope=MemoryReadScope.for_channel(1, CHANNEL))
    with pytest.raises(MemoryScopeError):
        await reader.retrieve("test", include_inactive=True)
    memory_rows.search.assert_not_awaited()


async def test_management_review_is_explicit_and_still_workspace_bounded(memory_rows: SimpleNamespace) -> None:
    reader = retriever.MemoryRetriever(1, scope=MemoryReadScope.for_workspace_review(1))
    memories = (await reader.retrieve("test", limit=30)).memories
    assert any(item.source_type == "paragraph" and item.target_id == 2 for item in memories)
    assert not any(item.source_type == "paragraph" and item.target_id == 5 for item in memories)
    assert memory_rows.search.await_args.kwargs["paragraph_ids"] is None


async def test_recent_and_entity_recall_use_the_same_scope(memory_rows: SimpleNamespace) -> None:
    reader = retriever.MemoryRetriever(1, scope=MemoryReadScope.for_channel(1, CHANNEL))
    assert [item.target_id for item in await reader.get_recent_memories()] == [1]
    assert all(item.origin_chat_key == CHANNEL for item in await reader.retrieve_by_entity("test", limit=20))


@pytest.mark.parametrize("memory_type,memory_id", [
    ("paragraph", 2), ("paragraph", 3), ("paragraph", 4), ("paragraph", 5),
    ("relation", 11), ("relation", 12), ("episode", 21), ("episode", 22), ("episode", 23), ("entity", 1),
])
async def test_guessed_detail_ids_and_unproven_aggregates_are_rejected(
    memory_rows: SimpleNamespace, memory_type: str, memory_id: int,
) -> None:
    with pytest.raises(MemoryScopeError, match="未找到可访问的记忆"):
        await require_visible_memory(1, memory_type, memory_id, MemoryReadScope.for_channel(1, CHANNEL))


@pytest.mark.parametrize("memory_type,memory_id", [("paragraph", 1), ("relation", 10), ("episode", 20)])
async def test_authorized_details_and_origin_trace_remain_available(
    memory_rows: SimpleNamespace, memory_type: str, memory_id: int,
) -> None:
    scope = MemoryReadScope.for_channel(1, CHANNEL)
    kind = query_service.MemoryObjectType(memory_type)
    detail = await query_service.get_workspace_memory_detail(1, kind, memory_id, scope=scope)
    trace = await query_service.trace_workspace_memory_origin(1, kind, memory_id, scope=scope)
    assert detail.memory_id == memory_id
    assert trace.origin_chat_key == CHANNEL


async def test_trace_and_search_cannot_be_used_without_scope(memory_rows: SimpleNamespace) -> None:
    with pytest.raises(MemoryScopeError):
        await query_service.trace_workspace_memory_origin(1, query_service.MemoryObjectType.PARAGRAPH, 1)
    with pytest.raises(MemoryScopeError):
        await query_service.search_workspace_memories(1, "test")
    memory_rows.search.assert_not_awaited()


async def test_qdrant_uses_authoritative_ids_before_candidate_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(search=AsyncMock(return_value=[]))
    monkeypatch.setattr(qdrant_manager, "get_qdrant_client", AsyncMock(return_value=client))
    await qdrant_manager.MemoryQdrantManager().search([0.1], 1, paragraph_ids=[1, 7])
    conditions = client.search.await_args.kwargs["query_filter"].must
    assert any(isinstance(condition, qdrant_models.HasIdCondition) and condition.has_id == [1, 7] for condition in conditions)


async def test_empty_id_scope_does_not_touch_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    get_client = AsyncMock()
    monkeypatch.setattr(qdrant_manager, "get_qdrant_client", get_client)
    assert await qdrant_manager.MemoryQdrantManager().search([0.1], 1, paragraph_ids=[]) == []
    get_client.assert_not_awaited()


async def test_online_history_passes_current_channel_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.agent.templates import history
    from nekro_agent.services.memory import routing

    monkeypatch.setattr(routing, "get_memory_route", AsyncMock(return_value=None))
    from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec

    memory = retriever.RetrievedMemory(
        target_id=1, source_type="paragraph", episode_id=None, paragraph_id=1, relation_id=None,
        content="safe", summary="safe", cognitive_type="episodic", knowledge_type="fact",
        similarity_score=0.9, effective_weight=1.0, event_time=None, origin_chat_key=CHANNEL,
    )
    recall = AsyncMock(return_value=[memory])
    monkeypatch.setattr(history, "is_memory_system_enabled", lambda: True)
    monkeypatch.setattr(history, "_build_memory_recall_plan", AsyncMock(
        return_value=MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="test")]),
    ))
    monkeypatch.setattr(retriever, "retrieve_memories", recall)
    monkeypatch.setattr(retriever, "compile_memories_for_context", AsyncMock(return_value="safe"))
    injection = await history._inject_memory_context(1, [], chat_key=CHANNEL)
    assert injection.text == "safe\n\n" and injection.history is None
    assert recall.await_args.kwargs["scope"] == MemoryReadScope.for_channel(1, CHANNEL)


async def test_authenticated_workspace_handshake_explicitly_selects_review_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.routers import workspaces

    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(workspaces, "is_memory_system_enabled", lambda: True)
    monkeypatch.setattr(workspaces, "retrieve_memories", recall)
    await workspaces._build_cc_memory_handshake(1, "test")
    assert recall.await_args.kwargs["scope"] == MemoryReadScope.for_workspace_review(1)
