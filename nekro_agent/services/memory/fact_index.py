"""可重建的事实向量投影；正文和最终读取权限始终由 SQL 决定。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict
from qdrant_client import models as qm
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.vector_db import get_qdrant_client
from nekro_agent.models.db_mem_fact import DBMemAssertion, DBMemIndexOutbox, DBMemScope
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.embedding_service import EmbeddingService

logger = get_sub_logger("memory.fact_index")


def _configuration_identity() -> str:
    group = config.MEMORY_EMBEDDING_MODEL_GROUP
    model = config.get_model_group_info(group)
    return hashlib.sha256(json.dumps(
        ["fact-v1", group, model.CHAT_MODEL, model.BASE_URL, int(config.MEMORY_EMBEDDING_DIMENSION)],
    ).encode()).hexdigest()[:20]


class FactIndexKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    assertion_id: UUID
    revision: int

    @property
    def point_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, f"nekro:fact:{self.assertion_id}:{self.revision}"))


class FactIndex(Protocol):
    async def upsert(self, key: FactIndexKey, text: str) -> None: ...

    async def delete(self, assertion_id: UUID) -> None: ...

    async def search(self, query: str, allowed: list[FactIndexKey], limit: int) -> list[FactIndexKey]: ...


class QdrantFactIndex:
    """独立 collection；配置变化创建新投影，绝不删除旧段落 collection。"""

    def __init__(self) -> None:
        group = config.MEMORY_EMBEDDING_MODEL_GROUP
        self.dimension = int(config.MEMORY_EMBEDDING_DIMENSION)
        self.identity = _configuration_identity()
        self.collection = f"nekro_facts_v1_{self.identity}"
        self.embedder = EmbeddingService(model_group=group, dimension=self.dimension)

    def _assert_configuration(self) -> None:
        if self.identity != _configuration_identity():
            raise MemoryError("INDEX_CONFIG_CHANGED")

    async def ensure(self) -> None:
        client = await get_qdrant_client()
        if not await client.collection_exists(self.collection):
            try:
                await client.create_collection(
                    self.collection, vectors_config=qm.VectorParams(size=self.dimension, distance=qm.Distance.COSINE),
                )
            except Exception:
                # 允许并发建表赢家；其它错误不能冒充初始化成功。
                if not await client.collection_exists(self.collection):
                    raise
        info = await client.get_collection(self.collection)
        vector = info.config.params.vectors
        if not isinstance(vector, qm.VectorParams) or vector.size != self.dimension or vector.distance != qm.Distance.COSINE:
            raise MemoryError("INDEX_CONFIG_MISMATCH")

    async def upsert(self, key: FactIndexKey, text: str) -> None:
        self._assert_configuration()
        vector = await self.embedder.embed_text(text)
        self._assert_configuration()
        await self.ensure()
        client = await get_qdrant_client()
        await client.upsert(self.collection, points=[qm.PointStruct(
            id=key.point_id, vector=vector,
            payload={"assertion_id": str(key.assertion_id), "revision": key.revision},
        )], wait=True)

    async def delete(self, assertion_id: UUID) -> None:
        client = await get_qdrant_client()
        if await client.collection_exists(self.collection):
            await client.delete(self.collection, points_selector=qm.FilterSelector(filter=qm.Filter(must=[
                qm.FieldCondition(key="assertion_id", match=qm.MatchValue(value=str(assertion_id))),
            ])), wait=True)

    async def search(self, query: str, allowed: list[FactIndexKey], limit: int) -> list[FactIndexKey]:
        if not allowed:
            return []
        client = await get_qdrant_client()
        if not await client.collection_exists(self.collection):
            return []
        self._assert_configuration()
        vector = await self.embedder.embed_text(query)
        self._assert_configuration()
        keys = {key.point_id: key for key in allowed}
        result = await client.query_points(
            self.collection, query=vector, query_filter=qm.Filter(must=[qm.HasIdCondition(has_id=list(keys))]),
            limit=limit, with_payload=False, with_vectors=False,
        )
        return [keys[str(point.id)] for point in result.points if str(point.id) in keys]


class IndexLease(BaseModel):
    id: UUID
    token: UUID
    assertion_id: UUID


class FactIndexWorker:
    def __init__(self, index: FactIndex) -> None:
        self.index = index

    async def claim(self, *, lease_seconds: int = 60) -> IndexLease | None:
        if not 1 <= lease_seconds <= 3600:
            raise MemoryError("LEASE_INVALID")
        now = datetime.now(timezone.utc)
        async with in_transaction() as conn:
            exhausted = DBMemIndexOutbox.filter(attempts__gte=5).filter(
                Q(state="pending") | Q(state="running", lease_until__lte=now),
            )
            await exhausted.using_db(conn).update(state="dead_letter", lease_token=None, lease_until=None)
            row = await DBMemIndexOutbox.filter(
                Q(state="pending") | Q(state="running", lease_until__lte=now),
                Q(next_retry_at=None) | Q(next_retry_at__lte=now), attempts__lt=5,
            ).using_db(conn).select_for_update(skip_locked=True).order_by("created_at", "id").first()
            if row is None:
                return None
            token = uuid4()
            await DBMemIndexOutbox.filter(id=row.id).using_db(conn).update(
                state="running", lease_token=token, lease_until=now + timedelta(seconds=lease_seconds),
                attempts=row.attempts + 1,
            )
            return IndexLease(id=row.id, token=token, assertion_id=row.assertion_id)

    async def process_one(self) -> bool:
        lease = await self.claim()
        if lease is None:
            return False
        from nekro_agent.services.memory.service import MemoryService

        try:
            # 事件顺序不代表事实版本顺序：始终投影 SQL 当前状态。
            row = await DBMemAssertion.filter(id=lease.assertion_id).first()
            now = datetime.now(timezone.utc)
            scope = await DBMemScope.filter(id=row.scope_id).first() if row else None
            readable = (
                row is not None and scope is not None and row.status == "active"
                and row.valid_from <= now and (row.valid_to is None or row.valid_to > now)
                and bool(await MemoryService()._valid_sources(row.id, scope))
            )
            if readable and row is not None:
                text = f"{row.predicate}: {json.dumps(row.value_json['value'], ensure_ascii=False)}"
                await asyncio.wait_for(self.index.upsert(
                    FactIndexKey(assertion_id=row.id, revision=row.row_revision), text,
                ), timeout=45)
            else:
                await asyncio.wait_for(self.index.delete(lease.assertion_id), timeout=45)
            await self._finish(lease, success=True)
        except Exception as error:
            logger.warning(f"事实索引任务降级: event={lease.id}, error_type={type(error).__name__}")
            await self._finish(lease, success=False)
        return True

    async def _finish(self, lease: IndexLease, *, success: bool) -> None:
        async with in_transaction() as conn:
            row = await DBMemIndexOutbox.filter(
                id=lease.id, state="running", lease_token=lease.token,
                lease_until__gt=datetime.now(timezone.utc),
            ).using_db(conn).select_for_update().first()
            if row is None:
                return
            await DBMemIndexOutbox.filter(id=row.id).using_db(conn).update(
                state="done" if success else "dead_letter" if row.attempts >= 5 else "pending",
                lease_token=None, lease_until=None,
                next_retry_at=None if success else datetime.now(timezone.utc) + timedelta(seconds=2 ** row.attempts),
            )

    async def rebuild(self) -> int:
        """显式重建入口；幂等向量写入，包含非活跃记录以清除旧投影。"""
        count = 0
        async for row in DBMemAssertion.all():
            await DBMemIndexOutbox.create(
                assertion_id=row.id, assertion_version=row.row_revision, operation="reconcile",
            )
            count += 1
        return count
