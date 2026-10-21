"""事实领域事件按消费者交接；不与向量索引确认状态混用。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel
from tortoise.expressions import Q, Subquery
from tortoise.transactions import in_transaction

from nekro_agent.models.db_mem_fact import DBMemDomainOutbox, DBMemEventDelivery
from nekro_agent.services.memory.contracts import MemoryError


class DeliveryLease(BaseModel):
    id: UUID
    event_id: UUID
    consumer_id: str
    token: UUID


class DomainEventBroker:
    """由服务端为已注册消费者传入明确 scope；本类不授予内容读取权限。"""

    async def prepare(self, *, consumer_id: str, scope_id: UUID, limit: int = 100) -> int:
        if not consumer_id or len(consumer_id) > 128 or limit < 1 or limit > 1000:
            raise MemoryError("CONSUMER_INVALID")
        # SQL anti-join 以外也不使用最近 N 条窗口或易跳过事务的全局游标。
        delivered = DBMemEventDelivery.filter(consumer_id=consumer_id).values("event_id")
        events = await DBMemDomainOutbox.filter(scope_id=scope_id).exclude(
            id__in=Subquery(delivered),
        ).order_by("created_at").limit(limit)
        created = 0
        for event in events:
            _, added = await DBMemEventDelivery.get_or_create(event_id=event.id, consumer_id=consumer_id)
            created += int(added)
        return created

    async def claim(self, *, consumer_id: str, lease_seconds: int = 60) -> DeliveryLease | None:
        if not consumer_id or len(consumer_id) > 128 or lease_seconds < 1 or lease_seconds > 3600:
            raise MemoryError("CONSUMER_INVALID")
        now = datetime.now(timezone.utc)
        async with in_transaction() as conn:
            delivery = await DBMemEventDelivery.filter(
                Q(state="pending") | Q(state="running", lease_until__lte=now),
                Q(next_retry_at=None) | Q(next_retry_at__lte=now), consumer_id=consumer_id,
            ).using_db(conn).select_for_update(skip_locked=True).order_by("id").first()
            if delivery is None:
                return None
            token = uuid4()
            await DBMemEventDelivery.filter(id=delivery.id).using_db(conn).update(
                state="running", lease_token=token, lease_until=now + timedelta(seconds=lease_seconds),
                attempts=delivery.attempts + 1,
            )
            return DeliveryLease(id=delivery.id, event_id=delivery.event_id, consumer_id=consumer_id, token=token)

    async def acknowledge(self, lease: DeliveryLease) -> None:
        count = await DBMemEventDelivery.filter(
            id=lease.id, event_id=lease.event_id, consumer_id=lease.consumer_id, state="running",
            lease_token=lease.token, lease_until__gt=datetime.now(timezone.utc),
        ).update(state="done", lease_token=None, lease_until=None)
        if not count:
            raise MemoryError("LEASE_LOST")

    async def retry(self, lease: DeliveryLease, *, delay_seconds: int = 2, max_attempts: int = 5) -> None:
        if delay_seconds < 0 or delay_seconds > 3600 or max_attempts < 1:
            raise MemoryError("RETRY_INVALID")
        async with in_transaction() as conn:
            row = await DBMemEventDelivery.filter(
                id=lease.id, consumer_id=lease.consumer_id, event_id=lease.event_id, state="running",
                lease_token=lease.token, lease_until__gt=datetime.now(timezone.utc),
            ).using_db(conn).select_for_update().first()
            if row is None:
                raise MemoryError("LEASE_LOST")
            await DBMemEventDelivery.filter(id=row.id).using_db(conn).update(
                state="dead_letter" if row.attempts >= max_attempts else "pending",
                lease_token=None, lease_until=None,
                next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay_seconds),
            )
