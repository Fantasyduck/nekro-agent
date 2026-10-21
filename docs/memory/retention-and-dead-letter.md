# 遗忘回执、正文清理与死信运维

本页记录第八阶段（一致性运维）落地的三件事：遗忘回执的四段状态、按保留期清理正文、
以及索引与抽取两条队列的死信重试与确认忽略。设计依据见
`outputs/nekro-memory-detailed-design-v1.md` 的 :635（ForgetReceipt 字段）、
:344/:346（任务状态与 dead_letter 处置）、:663-671（删除传播链）、:687/:689（已删除隐私内容的清理与保留期）。

## 1. 遗忘回执不再是一句「已删除」

`MemoryService.forget` 返回 `ForgetReceipt`，四个状态互相独立：

| 字段 | 含义 | 何时为真 |
| --- | --- | --- |
| `logical_block_committed` | 读路径已阻断 | 事务提交即 True |
| `affected_assertions` | 被置为 deleted 的版本数 | 同逻辑键的全部非 deleted 版本 |
| `derived_cleanup_pending` | 派生数据待重算 | 有版本被删 |
| `index_cleanup_pending` | 向量索引尚未撤除 | `mem_index_outbox` 仍有 pending/running/dead_letter 行 |
| `physical_purge_state` | 正文本体是否已清 | `disabled` / `scheduled` / `pending` / `completed` |

要点：逻辑阻断提交后立刻能说「后续不会再用于回答」，但不能说「已彻底清除」。
索引撤除由 `FactIndexWorker` 异步完成，因此刚遗忘时 `index_cleanup_pending` 通常为 True。

## 2. 物理清理按保留期执行

新增配置 `MEMORY_FACT_PURGE_AFTER_DAYS`（默认 0，范围 0..3650）：

- `0` 表示**不启用**物理清理，正文留在库里，仅保证不参与读取；
- 设为 N 天则在「遗忘操作满 N 天」后清除正文。

具体天数由部署方按隐私承诺决定，代码与文档都不猜默认值（设计 :689）。

`MemoryService.purge_forgotten(context, limit=50)`：

- 仅管理员 + `purpose="admin_review"`，否则 `SCOPE_DENIED`；`limit` 必须 1..500，否则 `LIMIT_INVALID`；
- 只处理 `status=deleted` 且未清理的断言，按 `mem_audit_event.operation="deleted"` 的时间判断是否到期；
- 事务内 `select_for_update` 复核状态仍是 deleted，再写
  `value_json={"value": None, "purged": True}`、`row_revision + 1`，并记审计 `fact.purged`；
- 只清正文，保留行、版本链、来源标识与审计，`purged` 标记让重复清理幂等。

调度：`MemoryFactRuntime.tick()` 在 `MEMORY_FACT_PURGE_AFTER_DAYS > 0` 时按 scope 调用；
该配置单独也能启动运行时（`start()` 的三个条件之一）。

## 3. 死信必须由管理动作处置

`nekro_agent/services/memory/dead_letter.py` 的 `DeadLetterAdmin`：

- `list(kind, state="dead_letter", limit, offset)`：`kind` 为 `index` 或 `ingest`，越界时报 `KIND_INVALID`；
  只能看到当前工作区 + 频道对应 scope 的记录，否则 `SCOPE_UNRESOLVED`；
- `retry(...)`：置回 `pending` 并把 `attempts` **归零**（否则下一轮领取立即再次判为穷尽）；
- `ignore(...)`：置 `cancelled`，保留记录与原因供对账，不默默算作成功；
- 两者都写审计 `index.retried` / `index.ignored` / `ingest.retried` / `ingest.ignored`。

响应只带编号、分类、次数与错误码，**不返回** `shadow_result` 或事实取值，避免运维接口泄漏正文。

## 4. 清理进度与旧 collection 生命周期

`MemoryService.retention_status(context)` 只返回数量、不返回任何正文，供运维判断是否需要干预：

| 键 | 含义 |
| --- | --- |
| `deleted_assertions` | 当前 scope 内已逻辑遗忘的断言数 |
| `purged_assertions` | 其中正文已清除的数量 |
| `pending_purge` | 尚未清除的数量（含保留期未到的） |
| `due_for_purge` | 启用保留期时，按遗忘审计时间已到期的数量 |
| `index_pending` | 索引队列里 pending/running 的条数 |
| `index_dead_letter` | 索引队列里 dead_letter 的条数 |

计数口径刻意区分「已清」与「到期可清」：`pending_purge` 长期不降但 `due_for_purge` 为 0，说明是保留期未到，
而不是清理任务没跑。

事实索引 collection 的生命周期由 `fact_index.list_fact_collections()` 与
`fact_index.purge_stale_fact_collections()` 管理：

- 只认 `nekro_facts_v1_` 前缀，**绝不触碰段落库**（段落 collection 另有归属）；
- `current` 由当前 embedding 配置算出的 identity 判定，配置一变 identity 就变，旧 collection 会被标为非 current；
- `points` 读取失败时返回 `None` 而不是 0，避免把「读不到」误报成「空 collection」；
- 清理时当前配置的 collection 永不删除，只删非 current 的历史投影。

## 5. 管理端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/memory_facts/dead_letters` | 按 kind/state 分页列死信 |
| POST | `/memory_facts/dead_letters/{item_id}/retry` | 重试 |
| POST | `/memory_facts/dead_letters/{item_id}/ignore` | 确认忽略 |
| POST | `/memory_facts/purge` | 立即执行一次正文清理（`limit` 1..500） |
| GET | `/memory_facts/retention` | 清理进度 + 事实索引 collection 概览 |
| POST | `/memory_facts/collections/purge` | 删除非 current 的历史 collection |

全部为管理员端点并受 `MemoryOperation.MANAGEMENT` 开关约束；`KIND_INVALID` 映射为 422。

## 6. 测试

`tests/test_memory_retention.py`（9 项）：

- 遗忘回执四段状态：索引任务仍在排队时 `index_cleanup_pending` 为 True，保留期未配置时 `physical_purge_state="disabled"`；
- 保留期未到不清理；把审计时间前移 30 天后再清理，正文变为 `{"value": None, "purged": True}`；重复清理返回 0 且只有一条 `fact.purged` 审计；
- 非管理员与非 admin_review 上下文被拒；`limit=0` 报 `LIMIT_INVALID`；
- 索引死信重试后 `state=pending, attempts=0`，忽略后 `state=cancelled`，两条审计齐备；
- 抽取死信查询不泄漏 `shadow_result` 正文；重试清空 `error_class`；
- 未知 kind 报 `KIND_INVALID`，跨工作区报 `SCOPE_UNRESOLVED`，非管理员构造即 `SCOPE_DENIED`；
- 清理进度随遗忘与清理变化：遗忘后 `pending_purge=1`、`due_for_purge=0`，审计时间前移后 `due_for_purge=1`，
  清理后 `purged_assertions=1` 且 `deleted_assertions` 仍为 1（行与审计保留）；非管理员读进度被拒；
- collection 生命周期：混入的段落库名字不被识别，非 current 的会被删除，current 的保留，读不到计数时为 `None`。

全量回归：455 passed / 2 skipped / 2 failed，失败仍是既有的
`test_plugin_call_priority`（jinja2 `TemplateNotFound: 'plugin.j2'`）与
`test_tool_result_continuation`（`KeyError: 'CHARACTER_PLUGIN_PATH'`）。