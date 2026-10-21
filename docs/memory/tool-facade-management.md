# 工具与管理入口统一（第五阶段）

本文记录第五阶段把 Agent 工具与管理页面收敛到统一事实服务的实现边界。它不是整体交付说明：
天使关系写入仍未收敛（见文末），那属于第六阶段。

## 1. 为什么工具不能直接 admin_edit

`MemoryService._observation` 对非 message 来源要求 `context.is_admin`，而 `is_admin` 只能由
服务端注入。因此普通 Agent 工具写入只有一条合法路径：绑定当前会话最近一条真实消息作为观察来源。
管理员路径（`admin_edit`）只开放给已完成鉴权的管理调用方。

副作用是正确的：工具声称「A 的时区是 UTC+8」但当前说话人是 B 时，`propose_fact` 的 pending
条件命中，事实进入 `pending_review`，工具文案必须如实说「已提交，等待确认」。

## 2. 新增模块

### core: `nekro_agent/services/memory/tool_facade.py`

| 函数 | 作用 |
|---|---|
| `require_service_route(chat_key)` | 未启用统一管线时抛 `PIPELINE_DISABLED`，避免写进没人读的库 |
| `latest_message_source(chat_key)` | 取最近一条非撤回、非 `sender_id=-1` 的消息，封装成 `SourceRef` 与 `message_revision` |
| `remember_fact(...)` / `remember_note(...)` | 结构化事实与自由文本笔记；笔记用多值谓词 `memory.note`，同主体多条互不覆盖；笔记可选 `title`/`origin` |
| `list_facts(...)` | 无关键词读 `list_facts`；有关键词走 `recall` 再取当前版本 |
| `forget_fact(...)` | 按断言 ID 逻辑遗忘，普通用户只能遗忘主体是自己的事实 |
| `admin_remember_fact(...)` / `admin_correct_fact(...)` | 管理员路径，来源 `admin_edit`，服务端注入 `is_admin` |
| `record_legacy_mapping(...)` / `resolve_legacy_mapping(...)` | 旧 `F{short_id}` 与新 `assertion_id` 的跳转表（`mem_legacy_map`） |

幂等键按来源消息 + 内容摘要生成，重复调用同一条工具不会反复产生版本。

### core: `nekro_agent/routers/memory_facts.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memory_facts` | 当前可见事实分页，支持 subject/predicate/status 过滤 |
| GET | `/api/memory_facts/{id}` | 单条事实的完整版本链（旧版本在前） |
| POST | `/api/memory_facts/{id}/correct` | 纠正取值，生成新版本并保留历史 |
| POST | `/api/memory_facts/{id}/forget` | 逻辑遗忘 |
| GET/POST/DELETE | `/api/memory_facts/grants` | 跨会话授权查询、授予与撤销 |

全部端点要求 `Role.Admin`，并先检查 `MEMORY_ENABLE_SYSTEM`。`MemoryError` 统一映射为应用错误：
`SCOPE_DENIED`/`SCOPE_UNRESOLVED` → 403（同一外观，不泄露存在性），
`SOURCE_INVALID`/`POLICY_CHANGED`/`VERSION_CONFLICT`/`IDEMPOTENCY_CONFLICT` → 409，
量纲类错误 → 400。

## 3. 插件侧

`nekro_memory_angel/service_bridge.py` 是插件调用统一服务的唯一入口，职责有两个：

1. 判定当前频道是否处于 service 模式（`get_memory_route(...).uses_service`）；
2. 把 `ResolutionResult` 翻译成如实的中文结论——`pending_review` 显示「已提交，等待确认」，
   绝不显示「已保存」。

已接入的写路径：

- `angel_remember`：service 频道按笔记提交，保留标签、重要度、好感度门槛与共享组说明；
- 「记住事实」工具：按结构化事实提交；
- 「查看事实」「删除事实」：读权威服务，删除支持 UUID 与旧 `F{short_id}`（经映射表）；
- `on_user_message` 的启发式「记住」：service 频道同样走服务；
- `user_routes.py` 管理面板的事实新增/纠正/删除：管理员路径走 `admin_edit`，纠正只允许改取值
  （改属性请新建），并要求旧编号已完成映射，否则明确报错而不是静默写进旧表。

## 4. 记忆与笔记读页

自由文本记忆（`angel_remember`）与结构化笔记（`angel_note_create`）共用多值谓词
`memory.note`，靠 value 里有没有 `title` 区分。这是有意的：两者都是「不参与精确查询、
供召回使用的长文本」，拆成两个谓词只会让召回需要枚举两次。

`tool_facade.remember_note` 因此增加两个可选参数：

| 参数 | 作用 |
|---|---|
| `title` | 非空时写入 `value["title"]`，并在幂等键里参与摘要，避免同正文不同标题互相吞并 |
| `origin` | 记录写入入口（`agent_tool` / `knowledge_grow` / `manual_admin`），只用于面板展示来源 |

面板侧 `service_bridge` 提供成对的读写函数：

| 函数 | 作用 |
|---|---|
| `panel_notes(kind="memories"|"notes")` | 读 `NOTE_PREDICATE` 的 active 事实，按有无 title 分流，投影成面板两种表格需要的字段 |
| `panel_correct_memory(...)` | 把改动合并进原 payload 后调 `admin_correct_fact`，未编辑的键原样保留 |
| `panel_delete_note(...)` | 逻辑遗忘；旧编号先查 `mem_legacy_map`，未映射则视为不存在 |
| `admin_remember_memory(...)` | 面板手工新增记忆，走管理员路径写 `manual_admin` 来源 |
| `remember_document(...)` / `read_note(...)` | 笔记工具的服务模式读写，编号支持 UUID 与旧短号 |

语义上有一条硬约束：**统一记忆没有「归档」状态**（`FactStatus` 只有
active/pending_review/superseded/retracted/deleted）。因此面板的归档入口在 service 频道
直接 422 并提示「请使用纠正或遗忘」，不会静默退回 SQLite 写一份面板可见、模型读不到的记忆。

## 5. 仍未完成

- 天使关系写入与关系事件仍完全独立（设计上保留，但表达提供器与校验器未接，见第六阶段）；
- `engine.py` 的自动整合（`consolidate`）在 service 频道只做关系抽取，记忆正文写入仍待接；
- 旧表的 `memories`/`notes` 行尚未批量迁移到权威事实（第七阶段），当前靠 `mem_legacy_map`
  在写路径上增量登记。

## 6. 验证

- 全目录 440 项在隔离 PostgreSQL 上通过、2 项跳过，2 项与本改动无关的既有失败
  （`test_plugin_call_priority` 的 jinja2 模板路径、`test_tool_result_continuation` 缺 `CHARACTER_PLUGIN_PATH`）；
- 插件侧 39 项通过（含新增桥接 9 项：面板投影、笔记分流、纠错保留未编辑键、跨 scope 拒绝、旧编号未迁移）；
- 核心 `tests/test_memory_tool_facade.py` 新增 2 项：标题参与幂等键、`list_facts` 能按笔记谓词过滤；
- 核心 poe lint 通过。
