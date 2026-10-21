"""记忆读取边界：频道回答默认只使用本频道，工作区审查必须显式声明。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class MemoryScopeError(ValueError):
    """读取范围缺失、跨工作区或来源无法证明时拒绝读取。"""


@dataclass(frozen=True)
class MemoryReadScope:
    """由服务端调用点构建的读取范围，不接受模型自行指定共享频道。

    M0 只支持当前频道与已认证的管理审查。跨频道 grant 尚未实现，
    因此工作区相同、亲密度或旧索引命中都不能扩大频道读取范围。
    """

    workspace_id: int
    purpose: Literal["channel", "workspace_review"]
    chat_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, int) or isinstance(self.workspace_id, bool) or self.workspace_id <= 0:
            raise MemoryScopeError("记忆读取范围无效")
        if self.purpose == "channel":
            if not isinstance(self.chat_key, str) or not self.chat_key or self.chat_key != self.chat_key.strip():
                raise MemoryScopeError("记忆读取需要明确的当前频道")
        elif self.purpose != "workspace_review" or self.chat_key is not None:
            raise MemoryScopeError("记忆读取范围无效")

    @classmethod
    def for_channel(cls, workspace_id: int, chat_key: str) -> MemoryReadScope:
        return cls(workspace_id=workspace_id, purpose="channel", chat_key=chat_key)

    @classmethod
    def for_workspace_review(cls, workspace_id: int) -> MemoryReadScope:
        """仅供已经完成管理员认证的管理调用点使用。"""
        return cls(workspace_id=workspace_id, purpose="workspace_review")

    @property
    def is_channel(self) -> bool:
        return self.purpose == "channel"

    def allows_origin(self, origin_chat_key: str | None) -> bool:
        return not self.is_channel or origin_chat_key == self.chat_key


def require_read_scope(workspace_id: int, scope: MemoryReadScope | None) -> MemoryReadScope:
    if scope is None or scope.workspace_id != workspace_id:
        raise MemoryScopeError("记忆读取需要匹配工作区的明确范围")
    return scope


async def require_visible_memory(
    workspace_id: int,
    memory_type: str,
    memory_id: int,
    scope: MemoryReadScope | None,
) -> None:
    """详情与原文展开先检查来源，防止通过猜测 ID 绕过搜索过滤。"""
    checked = require_read_scope(workspace_id, scope)
    if not checked.is_channel:
        return

    from nekro_agent.models.db_mem_episode import DBMemEpisode
    from nekro_agent.models.db_mem_paragraph import DBMemParagraph
    from nekro_agent.models.db_mem_relation import DBMemRelation

    denied = MemoryScopeError("未找到可访问的记忆")
    if memory_type == "paragraph":
        paragraph = await DBMemParagraph.get_or_none(
            id=memory_id, workspace_id=workspace_id, origin_chat_key=checked.chat_key, is_inactive=False,
        )
        if paragraph is None:
            raise denied
        return

    if memory_type == "relation":
        relation = await DBMemRelation.get_or_none(id=memory_id, workspace_id=workspace_id, is_inactive=False)
        if relation is None or relation.paragraph_id is None:
            raise denied
        await require_visible_memory(workspace_id, "paragraph", relation.paragraph_id, checked)
        return

    if memory_type == "episode":
        episode = await DBMemEpisode.get_or_none(
            id=memory_id, workspace_id=workspace_id, origin_chat_key=checked.chat_key, is_inactive=False,
        )
        if episode is None or not episode.paragraph_ids:
            raise denied
        ids = episode.paragraph_ids
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
            raise denied
        visible_ids = await DBMemParagraph.filter(
            workspace_id=workspace_id, id__in=ids, origin_chat_key=checked.chat_key, is_inactive=False,
        ).values_list("id", flat=True)
        if set(visible_ids) != set(ids):
            raise denied
        return

    # 实体画像是工作区聚合，M0 尚不能证明其全部字段都来自当前频道。
    raise denied
