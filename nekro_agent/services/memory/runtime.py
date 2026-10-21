"""统一事实任务的生命周期和补扫；默认不调用模型，不自动切换频道。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import DBMemScope
from nekro_agent.services.memory.contracts import MemoryError, SourceRef
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.ingest_worker import MemoryIngestQueue
from nekro_agent.services.memory.routing import MemoryRoute, get_memory_route
from nekro_agent.services.memory.service import message_revision

logger = get_sub_logger("memory.fact_runtime")
PIPELINE_VERSION = "self-facts-v1"


class MemoryFactRuntime:
    def __init__(self) -> None:
        self.queue = MemoryIngestQueue()
        self._task: asyncio.Task[None] | None = None
        # 只作单轮扫描分页，不作持久消费水位；完整扫描后归零，迟提交记录不会永久跳过。
        self._scan_after: dict[UUID, int] = {}

    async def enqueue_message(self, message_id: int) -> bool:
        if not is_memory_system_enabled() or not config.MEMORY_FACT_WORKER_ENABLED:
            return False
        message = await DBChatMessage.filter(id=message_id, is_recalled=False).first()
        if message is None or str(message.sender_id) == "-1":
            return False
        route = await get_memory_route(message.chat_key)
        if route is None or route.mode == "legacy_guarded":
            return False
        if not self._allowed(route):
            return False
        await self.queue.enqueue(
            route.context(), SourceRef(kind="message", id=str(message.id), revision=message_revision(message)),
            pipeline_version=PIPELINE_VERSION,
        )
        return True

    @staticmethod
    def _allowed(route: MemoryRoute) -> bool:
        return (
            is_memory_system_enabled() and bool(config.MEMORY_FACT_WORKER_ENABLED)
            and (route.mode == "service" or route.mode == "shadow" and bool(config.MEMORY_FACT_SHADOW_MODEL_ALLOWED))
        )

    async def scan_page(self, route: MemoryRoute, *, after_id: int = 0, page_size: int = 100) -> tuple[int, bool]:
        if page_size < 1 or page_size > 500 or after_id < 0:
            raise MemoryError("SCAN_LIMIT_INVALID")
        if not self._allowed(route):
            return after_id, False
        # 不取最近 N 条；从所有已提交消息分页补建缺失任务。周期重扫覆盖晚提交／修订。
        messages = await DBChatMessage.filter(
            chat_key=route.chat_key, id__gt=after_id, is_recalled=False,
        ).exclude(sender_id="-1").order_by("id").limit(page_size)
        for message in messages:
            await self.enqueue_message(message.id)
        return (messages[-1].id if messages else after_id), len(messages) == page_size

    async def tick(self) -> None:
        if not is_memory_system_enabled():
            return
        if config.MEMORY_FACT_INDEX_ENABLED:
            from nekro_agent.services.memory.fact_index import FactIndexWorker, QdrantFactIndex

            try:
                await FactIndexWorker(QdrantFactIndex()).process_one()
            except Exception as error:
                logger.warning(f"事实索引本轮降级: error_type={type(error).__name__}")
        await self._purge_if_due()
        if not config.MEMORY_FACT_WORKER_ENABLED:
            return
        from nekro_agent.services.memory.fact_extractor import extract_message_facts

        scopes = await DBMemScope.filter(
            agent_id=config.MEMORY_FACT_AGENT_ID, pipeline_mode__in=["shadow", "service"],
        ).order_by("id")
        for scope in scopes:
            try:
                route = await get_memory_route(scope.origin_chat_key)
                if route is None or route.scope_id != scope.id or not self._allowed(route):
                    continue
                cursor, more = await self.scan_page(route, after_id=self._scan_after.get(scope.id, 0))
                self._scan_after[scope.id] = cursor if more else 0
                await self.queue.process_one(scope_id=scope.id, extractor=extract_message_facts)
            except Exception as error:
                # 只记录分类，不将模型错误响应或消息正文写入普通日志。
                logger.warning(f"事实任务本轮降级: scope={scope.id}, error_type={type(error).__name__}")

    async def _purge_if_due(self) -> None:
        """保留期到达后清除已遗忘正文；未配置保留天数时完全不动数据。"""
        if int(config.MEMORY_FACT_PURGE_AFTER_DAYS) <= 0:
            return
        from nekro_agent.services.memory.contracts import RequestContext
        from nekro_agent.services.memory.service import MemoryService

        service = MemoryService()
        scopes = await DBMemScope.filter(agent_id=config.MEMORY_FACT_AGENT_ID).order_by("id")
        for scope in scopes:
            context = RequestContext(
                workspace_id=scope.workspace_id, agent_id=scope.agent_id, actor_key="system:retention",
                chat_key=scope.origin_chat_key, purpose="admin_review", is_admin=True,
            )
            try:
                purged = await service.purge_forgotten(context, limit=50)
                if purged:
                    logger.info(f"事实正文清理完成: scope={scope.id}, purged={purged}")
            except Exception as error:
                # 清理失败不影响读取阻断；下一轮重试。
                logger.warning(f"事实正文清理本轮降级: scope={scope.id}, error_type={type(error).__name__}")

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as error:
                logger.warning(f"事实任务调度暂不可用: error_type={type(error).__name__}")
            await asyncio.sleep(config.MEMORY_FACT_WORKER_INTERVAL_SECONDS)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not is_memory_system_enabled() or not (
            config.MEMORY_FACT_WORKER_ENABLED
            or config.MEMORY_FACT_INDEX_ENABLED
            or int(config.MEMORY_FACT_PURGE_AFTER_DAYS) > 0
        ):
            return
        self._task = asyncio.create_task(self._run(), name="memory-fact-runtime")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


memory_fact_runtime = MemoryFactRuntime()
