# 历史迁移：旧 SQLite 记录导入统一事实

第七阶段把天使插件 SQLite 里的**事实性数据**迁进统一事实服务。迁移只搬运事实，
不做语义重写：人格配置、好感度、关系阶段、关系事件与规则保留原权威（设计 §13）。

## 迁移什么

| 旧表 | 谓词 | 主体 | 备注 |
| --- | --- | --- | --- |
| `memories` | `memory.note` | `adapter:user_id`，空则 `adapter:channel` | 自由文本记忆，payload 无 `title` |
| `notes` | `memory.note` | `adapter:channel` | 结构化笔记，payload 带 `title` |
| `facts` | 原 `attribute` | `adapter:subject` 或 `adapter:user_id` | 属性事实，value 为原字符串 |

`memories` 与 `notes` 共用 `memory.note` 这个多值谓词：同一主体可以并存多条，
面板按 payload 里有没有 `title` 分流到「记忆」和「笔记」两页
（见 `service_bridge.panel_notes`）。

归档记录**不迁移**。统一事实只有 `active`、`pending_review`、`superseded`、
`retracted`、`deleted` 五种状态，没有 `archived`；把归档行原样搬过去会静默变成生效记忆。

## 为什么标 legacy_unverified

旧库记录没有可复核的消息锚点：没有 `chat_message.id`、没有行级 revision，
无法像正常写入那样验证「这条消息还在、还没被撤回、版本一致」。
设计允许的最低证据等级就是 `legacy_unverified`（detailed-design :888）：

- 记录正常参与召回，**不降权、不隐藏**；
- 但 `verification_status` 如实标成 `legacy_unverified`，不冒充 `user_declared` 或
  `manual_admin`；
- 面板与工具读出来的来源标签能区分「这条是迁过来的」。

## 来源种类

`SourceRef.kind` 新增 `legacy_import`（`contracts.py:39`）。服务端对它的处理：

- `_observation`：要求 `context.is_admin` 且 `purpose == "admin_review"`，否则
  `SCOPE_DENIED`；来源 ID 固定为 `legacy:<表>:<旧编号>`，同一行重复导入命中同一观察；
- `propose_fact`：`legacy = source.kind == "legacy_import" and context.is_admin`，
  命中后 `pending = False`（历史事实没有「等谁确认」的对象）；
- `_valid_sources`：放行，否则导入的行永远召回不到——这是最初设计里最容易漏掉的一环。

## 幂等与对账

`tool_facade.import_legacy_fact`（`:264`）是唯一入口：

```python
await tool_facade.import_legacy_fact(
    chat_key=..., actor_key=..., subject_key=..., predicate=..., value=...,
    legacy_table="memories" | "notes" | "facts", legacy_id=<旧行 id>,
    confidence=..., min_favor=..., checksum="",
)
```

- 幂等键 `legacy:<表>:<旧编号>:<checksum[:16]>`：同值重跑只命中幂等槽，不产生新版本；
- checksum 由 value 派生（可显式传入）：内容变了会写入新版本，并把
  `mem_legacy_map.import_status` 置为 `reimported`，便于对账找出差异；
- 行 id 与短号都登记映射（`record_legacy_mapping`），面板旧链接 `M12`、`F3` 仍可跳转；
- 频道未绑定工作区直接跳过并计入 `skipped_unbound`，**不回退到别的工作区**。

## 面板入口

`routes.py` 提供两个管理端点（`Role.Admin`）：

- `GET /migration/status`：每张旧表的 `total` / `migratable` / `mapped` / `pending`；
- `POST /migration/run?chat_key=&tables=memories,notes,facts&dry_run=false`：
  指定频道或全量迁移，返回逐表 `total/imported/created/supported/corrected/
  skipped_archived/skipped_empty/failed` 与最多 10 条错误摘要。

`dry_run=true` 只统计不写入，用于上线前核对数量。
