"""SQL 授权之后才融合候选；中文双字匹配不冒充数据库全文索引。"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from uuid import UUID

from nekro_agent.core.config import config
from nekro_agent.services.memory.contracts import EvidenceItem
from nekro_agent.services.memory.fact_index import FactIndex, FactIndexKey, QdrantFactIndex


def _grams(text: str) -> set[str]:
    compact = "".join(text.casefold().split())
    return {compact[index:index + 2] for index in range(len(compact) - 1)} if len(compact) > 1 else {compact}


async def rank_facts(
    query: str, items: list[EvidenceItem], *, limit: int, index: FactIndex | None = None,
) -> list[EvidenceItem]:
    if not items:
        return []
    literal: list[UUID] = []
    fuzzy: list[tuple[UUID, float]] = []
    terms = _grams(query)
    for item in items:
        text = json.dumps(item.value, ensure_ascii=False).casefold()
        if query.casefold() in text or query.casefold() in item.predicate.casefold():
            literal.append(item.assertion_id)
        coverage = len(terms & _grams(f"{item.predicate} {text}")) / max(1, len(terms))
        if coverage >= 0.5:
            fuzzy.append((item.assertion_id, coverage))
    routes = [literal, [key for key, _ in sorted(fuzzy, key=lambda pair: -pair[1])]]
    if index is not None or config.MEMORY_FACT_INDEX_ENABLED:
        try:
            backend = index if index is not None else QdrantFactIndex()
            # 外部检索只可命中已经过 SQL/来源/表达门控的当前版本。
            allowed = [FactIndexKey(assertion_id=item.assertion_id, revision=item.row_revision) for item in items[:1000]]
            hits = await asyncio.wait_for(backend.search(query[:2000], allowed, min(100, limit * 3)), timeout=3)
            permitted = {(key.assertion_id, key.revision) for key in allowed}
            routes.append(list(dict.fromkeys(
                key.assertion_id for key in hits if (key.assertion_id, key.revision) in permitted
            )))
        except Exception:
            # 降级不把上游异常响应或正文写日志；本地通路立即可读。
            pass
    scores: defaultdict[UUID, float] = defaultdict(float)
    for route in routes:
        for rank, assertion_id in enumerate(route, start=1):
            scores[assertion_id] += 1 / (60 + rank)
    canonical = {item.assertion_id: item for item in items}
    return [canonical[key] for key in sorted(scores, key=lambda key: -scores[key])[:limit]]
