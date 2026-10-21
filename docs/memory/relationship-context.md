# 关系协同：插件关系提供器与发送前复核

第六阶段（关系协同）把天使插件的关系状态接入核心 Agent 编排层。核心不 import 插件，
双方只通过一对回调耦合：插件的 `relationship_source` 向核心注册 provider 与 validator，
装配期产出一个 `RelationshipFragment`，发送前用同一个 validator 复核。

## 为什么需要这一层

统一记忆接管事实装配后，插件的 `render_prompt` 在 `route.uses_service` 时只返回工具说明
（`engine.py:487-490`）——事实由核心装配，但关系表达没有接手者。直接让插件继续拼旧画像不行：
旧路径按 `chat_key` 取档案，不区分当前受众，跨群聚合出来的分数与事件会漏进别的会话。

设计约束（detailed-design §11.7 / §11.8、§10.5 第 6 层）：

- 片段只包含当前受众可用信息；
- 表达提供器不得二次调用核心事实检索（禁止嵌套召回）；
- revision 可复算，发送前复核不一致就走有限重解析，而不是把旧片段发出去；
- 关系库不可用时给中性表达，不能阻塞发送。

## 装配与复核

注册表在 `nekro_agent/services/agent/memory_context.py`：

```python
register_relationship_source(*, provider, validator, label="")
clear_relationship_source(*, label=None) -> bool
get_relationship_source() -> (provider, validator)
```

- `MemoryContextBuilder.build()`（:192-209）只在 provider 与 validator 都在时尝试；
  provider 超时 0.5s 或抛异常只记 `relationship:unavailable` 并丢弃该层，不影响其余分层。
- `MemoryContextBuilder.validate()`（:221-227）在 `validate_bundle` 之后复核关系，
  返回 False 抛 `MemoryError("RELATIONSHIP_POLICY_CHANGED")`；
  `memory_send_guard.py:135` 据此走一次有限重解析，仍失败则剥离关系层再发。

装配点 `templates/history.py:374-379` 传的 `actor_key` 恒为 `service:answer`，不含发言人，
所以提供器必须自己解析「当前对话对象」。

## 插件实现（`nekro_memory_angel/relationship_source.py`）

| 关注点 | 做法 |
| --- | --- |
| 当前对象 | `DBChatMessage.filter(chat_key, is_recalled=False).exclude(sender_id="-1").order_by("-id")` 取最近一条真实用户消息 |
| 受众 | `{chat_key} ∪ sharing.members_of(chat_key, "share_memory")`；共享配置读不到时降级为仅本会话 |
| 事件过滤 | 事件的 `source_chat_key`（即 `origin_chat_key`）不在受众内、或来源为空，直接丢弃 |
| 片段文本 | 四行固定结构（长期关系说明／当前对象／关系阶段／互动基调）+ 关系刻度 + 稳定印象 + 标签 + 最近事件 + 边界提示 |
| revision | `sha256` 摘要 user_id、stage、score、权重、event_count、updated_at、summary、interaction_hint、tags 与可见事件；任一变化即换新值 |
| 复核 | 从 `source_refs` 里的 `favor:<user_id>` 取回装配时的对象，重算受众与状态，比对 revision |
| 不编造 | `ENABLE_FAVORABILITY` 关闭、无发言人、无档案时返回 `None`，不产出中性关系占位 |

`source_refs` 同时写入 `favor:<user_id>` 与 `favor-event:<kind>:<created_at>`，前者固定身份，
后者供审计追溯。

关系刻度（`engine.relationship_metrics`）要扫最近 2000 条消息，单独给了 0.2s 超时：
核心整个提供器的预算只有 0.5s，不能让「锦上添花」的统计把它吃满，超时就不显示刻度。

## 仍然待办

- 关系事件的跨库订阅：`mem_domain_outbox` 产生的 `fact.*` 事件还没有消费者确认，
  关系侧也尚未按事实撤回／来源删除触发依赖重算。
- 共享组变更后的失效：`storage.configure_relation_sharing` 会重算归并，但已生成的片段
  依赖发送前复核兜底，没有主动失效通知。

## 测试

- 插件：`tests/test_relationship_source.py` 10 项纯逻辑测试（revision 稳定性与变化检测、
  受众过滤、共享读取失败降级、渲染四行且不含成员清单、注册标签、复核在状态变化／越权事件／
  配置关闭时拒绝）；`work\run-plugin-tests.ps1` → 49 passed。
- 核心：`tests/test_memory_context_builder.py:113-146` 已覆盖注册表路径
  （注册后 build 生效、非法回调被拒、provider 失败走中性上下文）；全量
  `work\run-memory-postgres-tests.ps1 /workspace/tests` → 440 passed / 2 skipped / 2 既有失败。
