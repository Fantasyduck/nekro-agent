# 群聊理解能力增强设计稿

> 状态：待评审
> 版本：v0.1
> 目标仓库：`nekro-agent-core`（fork: Fantasyduck/nekro-agent，分支 `local/upgrade`）
> 相关：`UPGRADE_PLAN.md` 板块三 · 3.2 子 Agent / 上下文压缩（本文提供二者缺失的前置条件）

---

## 1. 背景

群里使用 Agent 时，反复出现两类抱怨：

- **回错人**：Agent 把 A 问 B 的话当成对自己的提问去回答，或答非所问地插话
- **错意**：Agent 理解了字面意思，但没抓住对话的实际指向（谁在跟谁说）

本设计稿不试图「让模型更聪明」，而是**把模型判断群聊所必需的信息补进 prompt**。
当前这些信息在渲染历史时**要么被丢弃、要么从未采集**，模型只能靠猜。

---

## 2. 实测证据

### 2.1 取证对象

线上真实群 `onebot_v11-group_1093792970`，bot 平台 ID `256143836`。
样本：`runtime/logs/prompts/chat_log_20261021_002333_495081.json`

渲染出的历史片段（实际产物，未加工）：

```
Recent Messages:
(role:user)[10-21 00:19:39 id:2497288407] "我要成为上潮高手" 说: <afb7f9f0 | At:[@id:3889650061;nickname:爱弥斯@]> #今日老婆
(role:user)[10-21 00:19:43 id:3889650061] "爱弥斯" 说: <afb7f9f0 | At:[@id:2497288407;nickname:我要成为上潮高手@>] 你今天的老婆是**露帕**
(role:user)[10-21 00:20:47 id:2453181507] "^^" 说: <afb7f9f0 | At:[@id:3889650061;nickname:爱弥斯@]> #今日老婆群抽取列表
(role:user)[10-21 00:20:48 id:3889650061] "爱弥斯" 说: 本群今日老婆抽取列表 > 1.^^→[莉莉[鸣潮]](...)
...
```

### 2.2 模型拿到的身份信息

| 信息 | 是否存在 | 来源 |
|---|---|---|
| 发送者昵称 | ✅ | `"..." 说:` |
| 发送者平台 ID | ✅ | `id:<platform_userid>` |
| 时间 | ✅ | `[mm-dd HH:MM:SS]` |
| 消息正文 | ✅ | 段内容 |
| @ 目标（ID+昵称） | ✅ | `<At:[@id:...;nickname:...@]>` |
| **发言者角色** | ✅ | `role:user/assistant/system`（本次升级新增） |
| **消息 ID** | ❌ 0 次 | `AI_ALWAYS_INCLUDE_MSG_ID = False` |
| **回复关系** | ❌ 0 次 | 依赖上一项 |
| **@我 / 回复我** | ❌ 0 次 | `AI_INCLUDE_TOME_INDICATOR = False` |
| **触发消息位置** | ❌ 无法定位 | 见 2.4 |
| **参与者名册** | ❌ | 无 |
| **触发原因** | ❌ | 无 |

> 计数方式：对渲染出的 `Recent Messages` 段做正则统计，`msg_id:` / `ref:` / `tome:true` 出现次数均为 **0**。

### 2.3 关键场景：@ 别人的消息唤醒了 bot

该群窗口内的 @ 目标频次：

```
@3889650061 (爱弥斯)      × 6
@245916572                × 1
@1842790021                × 1
@2497288407                × 1
@3160107723                × 1
@256143836 (bot 自己)      × 0    ← 一次都没有
```

同时：
- bot 在该窗口内**从未发过言**（6 个参与者中不含它）
- 触发消息是爱弥斯 `@九镑十五便士` 的那条

**这是一次「消息不是对 bot 说的，bot 却被唤起」的典型场景。**
模型此时应当知道「这条是对别人说的」，但 prompt 里没有任何信号能支撑这个判断。

### 2.4 已提交实现的缺陷（`turn_identity` 未闭环）

本次升级新增的 `<turn_identity>` 块：

```json
{"self_platform_id":"256143836","trigger":"message","sender_platform_id":"3889650061","message_id":"420873082"}
```

问题：历史行渲染的是 `id:{platform_userid}`（**用户 ID**），不是消息 ID。
全段检索 `420873082` —— **只出现在 `turn_identity` 自身，历史中零命中**。

后果：模型拿到一个无法对应的 message_id，只能靠 `sender_platform_id` 猜「爱弥斯说的最后一句」，
而爱弥斯在该窗口内出现了 **12 次**。

> 这条 `message_id` 字段在当前设计下是**负资产**，应删除或替换为历史中可定位的锚点。

### 2.5 上下文预算

| 配置 | 值 | 实测影响 |
|---|---|---|
| `AI_CONTEXT_LENGTH_PER_MESSAGE` | 768 | 13 条中 **3 条被截断**（`content too long, omitted`） |
| `AI_CONTEXT_LENGTH_PER_SESSION` | 5120 | 历史段实测 5,575 字符（13 条） |
| `AI_CHAT_CONTEXT_MAX_LENGTH` | 32 | 按条数截断（`history.py:530` 实际取 ×3=96 条再按字符裁） |

群聊里多为短句，768 单条上限对多数消息够用；但**卡片类长消息**（如「今日老婆抽取列表」）被拦腰砍断，
模型看到的是半句话。

### 2.6 防抖与触发链路

```
message_service.py:333-341   计算触发条件
  explicit_triggered = trigger_agent | FORCE_TRIGGER | preset.name in text | message.is_tome
  random_triggered   = random_chat_check(config)
  content_triggered  = check_content_trigger(text, config)
message_service.py:139        asyncio.create_task(_debounce_task(...))   ← 防抖
message_service.py:131        pending_messages[chat_key] = message        ← 只保留最后一条
message_service.py:232        run_agent(chat_key, chat_message, ctx)
```

两个结论：
1. **触发原因在 `:333-341` 算出来后被丢弃**——模型永远不知道自己为什么被叫醒
2. 防抖窗口内**只保留最后一条**作为触发消息。群聊连发时，`@` 可能落在**更早**的那条上。
   → 指向性标记必须覆盖**全部消息**，不能只标触发消息

---

## 3. 目标与非目标

### 3.1 目标

| # | 目标 | 可验证标志 |
|---|---|---|
| G1 | 模型能定位「当前该回应哪条消息」 | 历史中出现可定位的触发标记 |
| G2 | 模型能判断「这条消息是不是对我说的」 | 每条消息带 `at_me` / `reply_to_me` |
| G3 | 模型知道「谁在回复谁」 | 每条消息带 `reply_to=<id>` |
| G4 | 模型知道「我为什么被叫醒」 | `turn_identity.trigger_reason` |
| G5 | 模型能区分同名的不同人 | 参与者名册（id ↔ 昵称） |
| G6 | 群体里不抢话、不答非所问 | 契约给出可执行的「该不该接话」判定 |

### 3.2 非目标

- 不做情感/立场判断（那是记忆插件的领域）
- 不做多 Agent 协作（3.2 另议）
- 不改适配器协议、不要求各适配器新增字段
- 不改 `send_msg_text` 等沙盒方法的签名

---

## 4. 设计总览

六层改动，**互相独立、可单独开关**：

```
┌─ L6 群聊契约 ──────────── runtime_contract.j2：把「注意发言对象」写成可执行判定
├─ L5 上下文预算 ────────── 群聊按条数优先，长消息单独放宽
├─ L4 触发原因 ──────────── turn_identity 增加 trigger_reason
├─ L3 触发消息定位 ──────── 历史行加 trigger 标记
├─ L2 指向性标记 ────────── at_me / reply_to_me / reply_to=<id>
└─ L1 参与者名册 ────────── <participants> 块
```

数据流：

```
消息采集(已具备)
  ├─ content_data[].At.target_platform_userid   → at_me
  ├─ ext_data.ref_msg_id                        → reply_to
  ├─ is_tome                                    → at_me 兜底
  └─ sender_id / platform_userid                → role / 名册
        │
        ▼
  group_context.py（新增）：一次性构建 GroupTurnContext
        │
        ├─► render_history_data：渲染名册、消息标记、turn_identity
        └─► runtime_contract：注入「该不该接话」判定规则
```

---

## 5. 详细设计

### L1 · 参与者名册

**动机**：昵称可能重名、可能变更；模型每次都要从 `id:` 反查是谁。

**格式**（插在 `Recent Messages:` 之前）：

```
<participants>
2497288407=我要成为上潮高手
3889650061=爱弥斯
2453181507=^^
1842790021=烟花易冷 ༗࿐ོ
256143836=<你>
</participants>
```

**规则**：
- 只列**当前窗口内**出现过的参与者 + bot 自己
- 按首次出现顺序
- bot 自身固定标注 `<你>`
- 昵称为空时退化为 `id:<userid>`

**成本**：6 人 ≈ 195 字符。

**兼容性**：新增独立块，不改动任何既有行格式。

---

### L2 · 指向性标记

**动机**：这是「回错人」的直接解药。

**格式**（并入既有前缀括号，与现有 `msg_id:` / `ref:` / `tome:true` 同风格）：

```
(role:user,at_me)[10-21 00:20:00 id:2453181507] "^^" 说: ...
(role:user,reply_to=3889650061)[10-21 00:23:16 id:3160107723] "九镑十五便士" 说: ...
(role:user,reply_to_me)[10-21 00:23:20 id:1842790021] "烟花易冷" 说: ...
```

**判定规则**：

| 标记 | 判定 | 数据来源 |
|---|---|---|
| `at_me` | content_data 中存在 `ChatMessageSegmentAt.target_platform_userid == bot_platform_id` | 已采集 |
| `at_me`（兜底） | `is_tome == 1`（适配器未产出 At 段时） | 已采集 |
| `reply_to=<id>` | `ext_data.ref_msg_id` 解析出被回复者 | 已采集 |
| `reply_to_me` | 被回复者的 `platform_userid == bot_platform_id` | 需窗口内可解析 |

> **onebot 特殊说明**：`onebot_v11/tools/convertor.py:1058-1068` 在 `msg_to_me` 时会**在消息头部插入一个指向 bot 的 At 段**，
> 因此 `at_me` 在 onebot 上可稳定从 content_data 得出，不依赖适配器各自的 `is_tome` 语义。

**被回复者不在窗口内时**：降级为 `reply_to=?`，并在名册后补一行
`（部分被引用的消息已超出上下文窗口，无法确定被回复者）`。**不静默丢弃**。

**成本**：每条约 +14 字符 × 13 条 ≈ 180 字符。

---

### L3 · 触发消息定位

**动机**：现状 `turn_identity.message_id` 在历史中零命中（见 2.4）。

**方案**：在**历史行本身**加标记，与 L2 的前缀风格统一。

```
(role:user,trigger)[10-21 00:23:16 id:3889650061] "爱弥斯" 说: ...
```

同时：
- `turn_identity` 中**删除 `message_id`**（负资产）
- `turn_identity` 保留 `sender_platform_id` / `sender_nickname`，并加 `history_hint`

**为什么不用 `msg_id:`**：
`msg_id:` 需要开 `AI_ALWAYS_INCLUDE_MSG_ID`，会给**每条**消息加 ID（13 条 ≈ 260 字符），
而模型真正需要的只是「哪一条是触发」和「这条回复了谁」两个位置信息。
按需标记比全量 ID 便宜。

> **兼容性硬约束**：`agent_observer/__init__.py:185` 的 `_SPEECH_RE` 形如
> `\[(...time...) id:(\d+)\]\s*(?:"([^"]+)"|...)\s*[说:：]`，
> 依赖 `]` 紧跟 `"昵称"`。因此**所有新标记必须放在 `[` 之前的前缀括号里**，
> 绝不能插在 `]` 与昵称之间。`<participants>` 块同理不能插入行内。

---

### L4 · 触发原因

**动机**：2.6 指出触发原因被丢弃。模型不知道「为什么是我」。

**格式**：

```json
{
  "self_platform_id": "256143836",
  "trigger": "message",
  "trigger_reason": "at_me",
  "sender_platform_id": "3889650061",
  "sender_nickname": "爱弥斯",
  "at_me": false,
  "reply_to_me": false
}
```

**`trigger_reason` 取值**（对应 `message_service.py:333-341` 的四个分支）：

| 值 | 含义 | 对应代码 |
|---|---|---|
| `at_me` | 消息 @ 了 bot | `message.is_tome` |
| `reply_to_me` | 消息回复了 bot | `ref_msg_id` 指向 bot |
| `name_mentioned` | 正文含 bot 人设名 | `preset.name in message.content_text` |
| `forced` | 适配器/插件强制 | `trigger_agent` / `MsgSignal.FORCE_TRIGGER` |
| `keyword` | 命中触发词 | `check_content_trigger` |
| `random` | 随机接话 | `random_chat_check` |
| `unknown` | 无法归因 | 兜底 |

> `random` 这一档尤其重要：它意味着**这条消息可能根本不是对 bot 说的**，
> 契约应据此提示模型「除非消息明确向你提问，否则不必接话」。

**传递路径**：`message_service.py:333-341` 已算出四个布尔量，
新增一个局部变量 `trigger_reason` 并透传到 `run_agent`（新增可选关键字参数）。

---

### L5 · 群聊契约

**动机**：`runtime_contract.j2:7` 现在只有一句空话：

> You are in a multi-user chat environment. Always be aware of who sent each message.

而 `:27` 的 CoT 要求「Which messages have been replied to」——**数据并不在 prompt 里**，这一问必然靠猜。

**改写为可执行判定**（英文，保持与现有模板一致）：

```
## Group Conversation Protocol

You are one participant among several. Use the markers on each history line:

- `role:assistant` = your own past output. Never reply to it.
- `role:system` = tool/notice context. Never a user request.
- `at_me` = this message explicitly mentions you. It is a request to you.
- `reply_to_me` = this message replies to one of your messages. It is a request to you.
- `reply_to=<id>` = this message replies to that participant. `<participants>` maps ids to names.
- `trigger` = the message that woke you up this turn.

Rules:
1. Answer only messages marked `at_me`, `reply_to_me`, or `trigger` with `trigger_reason` of
   `at_me`/`reply_to_me`/`name_mentioned`/`keyword`/`forced`.
2. If `trigger_reason` is `random` and no message is marked `at_me` or `reply_to_me`,
   the message was probably not addressed to you. Stay silent unless it clearly asks the room a question.
3. Never answer a message that carries `reply_to=<id>` where `<id>` is not you, unless it also
   contains a question for everyone.
4. When you reply to a specific person in a group, address them with `[@id:<their id>@]`.
5. Do not re-ask questions already answered in `role:assistant` lines.
```

> 第 4 条依赖 `adapter.config.SESSION_ENABLE_AT = True`（当前 onebot 已开）与群聊场景
> （`at_parser.py:64` 私聊会丢弃 @）。契约文本按 `enable_at` 条件插入，与现有 `:12-20` 的写法一致。

---

### L6 · 上下文预算

| 项 | 现状 | 建议 | 理由 |
|---|---|---|---|
| `AI_CONTEXT_LENGTH_PER_MESSAGE` | 768 | 群聊保持 768，但**卡片类消息**单独放宽到 2048 | 长列表被腰斩导致「错意」 |
| `AI_CONTEXT_LENGTH_PER_SESSION` | 5120 | 群聊提到 8192 | 新增标记后净增约 8%，需留余量 |
| 历史条数 | 32 | 群聊提到 48 | 群聊轮次多，32 条常覆盖不了一轮对话 |

**实现**：按 `chat_type` 区分上限（私聊维持现值）。

---

## 6. 渲染格式规范（改动前后对照）

**改动前**：

```
Recent Messages:
(role:user)[10-21 00:19:39 id:2497288407] "我要成为上潮高手" 说: <...At:[@id:3889650061...]> #今日老婆
(role:user)[10-21 00:19:43 id:3889650061] "爱弥斯" 说: <...At:[@id:2497288407...]> 你今天的老婆是**露帕**
...
<turn_identity>{"self_platform_id":"256143836","trigger":"message","sender_platform_id":"3889650061","message_id":"420873082"}</turn_identity>
Only messages from other participants are reply targets. ...
```

**改动后**：

```
<participants>
2497288407=我要成为上潮高手
3889650061=爱弥斯
2453181507=^^
1842790021=烟花易冷 ༗࿐ོ
3160107723=九镑十五便士
256143836=<你>
</participants>

Recent Messages:
(role:user)[10-21 00:19:39 id:2497288407] "我要成为上潮高手" 说: <...At:[@id:3889650061...]> #今日老婆
(role:user)[10-21 00:19:43 id:3889650061] "爱弥斯" 说: <...At:[@id:2497288407...]> 你今天的老婆是**露帕**
(role:user,reply_to=2497288407)[10-21 00:20:48 id:3889650061] "爱弥斯" 说: 本群今日老婆抽取列表 ...
(role:user,at_me)[10-21 00:20:57 id:1842790021] "烟花易冷 ༗࿐ོ" 说: <...At:[@id:3889650061...]> #抢老婆...
(role:user,trigger)[10-21 00:23:16 id:3889650061] "爱弥斯" 说: <...At:[@id:3160107723...]> 成功签到 ...
...
<turn_identity>{"self_platform_id":"256143836","trigger":"message","trigger_reason":"random","sender_platform_id":"3889650061","sender_nickname":"爱弥斯","at_me":false,"reply_to_me":false}</turn_identity>
Only messages from other participants are reply targets. ...
```

**净增字符**（按本样本 13 条 / 6 人计）：

| 项 | 增量 |
|---|---|
| `<participants>` 块 | +195 |
| 每条标记（均摊 2 个标记） | +180 |
| `turn_identity` 改写 | +40 |
| **合计** | **≈ +415 字符（占 5,120 预算的 8.1%）** |

---

## 7. 改动清单

### 7.1 新增文件（集中在新目录，便于上游 merge）

| 文件 | 职责 |
|---|---|
| `nekro_agent/services/agent/templates/group_context.py` | `GroupTurnContext` 数据结构、构建逻辑、ContextVar 存取 |
| `nekro_agent/services/agent/templates/group_protocol.py` | 群聊契约文本（与 `runtime_contract.j2` 分离，便于独立迭代） |
| `tests/test_group_context.py` | 单元测试 |

### 7.2 修改文件

| 文件:行 | 改动 | 侵入性 |
|---|---|---|
| `models/db_chat_message.py:51-78` | `parse_chat_history_prompt` 读取 ContextVar，追加方向标记 | **不改签名** |
| `models/db_chat_message.py:74-76` | 现有 `role:` 前缀扩展为逗号分隔多标记 | 局部 |
| `services/agent/templates/history.py:506-531` | 新增 `group_context` 组装与名册注入 | 加局部变量 |
| `services/agent/templates/history.py:663-700` | 渲染循环传入方向信息 | 局部 |
| `services/agent/templates/history.py:487-506` | `_render_turn_identity` 删除 `message_id`、增加 `trigger_reason` | 局部 |
| `services/agent/run_agent.py:51` | `run_agent` 增加可选 `trigger_reason` 关键字参数 | **新增可选参数，不改已有参数** |
| `services/message_service.py:333-341` | 计算 `trigger_reason` 并透传 | 局部 |
| `services/agent/templates/j2/runtime_contract.j2:7` | 替换空话为可执行判定 | 文本 |
| `services/agent/templates/j2_zh/runtime_contract.j2` | 同上（中文模板同步） | 文本 |
| `core/config.py` | 新增 4 个开关 + 2 个群聊预算项 | 新增字段 |

> **模板双份**：`templates/j2/`（英文，本频道在用）与 `templates/j2_zh/`（中文）必须同步修改。

### 7.3 签名影响评估

| 函数 | 变化 | 风险 |
|---|---|---|
| `parse_chat_history_prompt` | **不变**（用 ContextVar 传上下文） | 无 |
| `render_history_data` | 内部新增局部变量，**签名不变** | 无 |
| `run_agent` | 新增 `trigger_reason: str = ""` 可选关键字 | 极低（core 内部函数，插件不调用） |
| `message_service.schedule_agent_task` | 不变 | 无 |

**为什么用 ContextVar 而不是给 `parse_chat_history_prompt` 加参数**：
该方法是 `DBChatMessage` 的公开方法，上游可能有其他调用方；
ContextVar 让新增能力**完全收敛在新目录**，合并上游时冲突面最小。
且 `asyncio.create_task`（`message_service.py:139`）会复制 Context，跨任务传递安全。

---

## 8. 配置项

新增于 `nekro_agent/core/config.py`：

```python
AI_GROUP_ROSTER_ENABLED: bool = True
"""群聊参与者名册。关闭后不注入 <participants> 块。"""

AI_GROUP_DIRECTION_ENABLED: bool = True
"""群聊指向性标记：at_me / reply_to_me / reply_to=<id>。"""

AI_GROUP_TRIGGER_MARKER_ENABLED: bool = True
"""群聊触发消息标记：历史行的 trigger 标记。"""

AI_GROUP_PROTOCOL_ENABLED: bool = True
"""群聊契约文本（Group Conversation Protocol）。"""

AI_GROUP_CONTEXT_LENGTH_PER_MESSAGE: int = 2048
"""群聊单条消息字符上限（私聊仍用 AI_CONTEXT_LENGTH_PER_MESSAGE）。"""

AI_GROUP_CONTEXT_LENGTH_PER_SESSION: int = 8192
"""群聊会话历史字符上限（私聊仍用 AI_CONTEXT_LENGTH_PER_SESSION）。"""
```

> 全部默认 `True`，但可通过 `nekro-agent.yaml` 逐项关闭，便于**线上灰度与快速回滚**。

---

## 9. 验证方案

### 9.1 单元测试（`tests/test_group_context.py`）

| 用例 | 断言 |
|---|---|
| `at_me` 来自 At 段 | content_data 含 bot 的 At → 标记出现 |
| `at_me` 兜底 | 无 At 段但 `is_tome=1` → 标记出现 |
| `reply_to` 解析 | ref_msg_id 指向窗口内消息 → `reply_to=<id>` |
| `reply_to_me` | ref 指向 bot 自己的消息 → `reply_to_me` |
| `reply_to` 目标出窗 | → `reply_to=?` + 提示行，**不静默丢弃** |
| `trigger` 标记 | 触发消息行带 `trigger` |
| 名册 | 含全部窗口内参与者 + `<你>` |
| **向后兼容** | observer 的 `_SPEECH_RE` 仍能匹配新格式行 |
| 私聊不注入 | `chat_type=private` → 无名册、无方向标记 |

> 最后一条「向后兼容」用 observer 的真实正则做断言，防止插件侧解析被打断。

### 9.2 回归渲染比对

取线上样本 `chat_log_20261021_002333_495081.json`，
用改造后的代码重渲染 `Recent Messages`，输出**前后 diff** 供人工核对。

### 9.3 离线评测（复用已有 Eval 框架）

现有框架：`nekro_agent/services/eval/`（`case.py` / `runner.py` / `grader.py` / `report.py`），
fixture 按场景分类（`单工具B` / `图片消息D` / `工具调用A` / `引用消息E` / `纯文本F` / `长上下文C`）。

**新增场景类别：`群聊指向G`**

从线上群聊日志抽取 N 条触发样本，人工标注三个问题：

1. 这条消息是对谁说的？（被回复者 id）
2. 这条消息是对 bot 说的吗？（是/否）
3. bot 这次该不该接话？（是/否）

评测方式：同一批 prompt 渲染两版（改造前 / 改造后），
用 `grader.py` 统计三问的准确率。

**验收标准**（建议值，待评审确认）：

| 指标 | 改造前（实测基线） | 改造后目标 |
|---|---|---|
| 被回复者识别准确率 | 待测 | ≥ 90% |
| 「是否对 bot 说话」判断准确率 | 待测 | ≥ 85% |
| 「该不该接话」判断准确率 | 待测 | ≥ 80% |

> 基线必须**先测出来**再改，不能凭直觉声称提升。
> 改造前的基线预计很低（因为信息根本不在 prompt 里）——这正是本设计的价值论证。

### 9.4 线上灰度

1. 先开 `AI_GROUP_DIRECTION_ENABLED` 单项，观察 24h
2. 再开 `AI_GROUP_ROSTER_ENABLED` + `AI_GROUP_TRIGGER_MARKER_ENABLED`
3. 最后开 `AI_GROUP_PROTOCOL_ENABLED`

每步观察：observer 面板的 `cache_hit_rate`（prompt 变长会略降）、群聊投诉反馈。

---

## 10. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| prompt 变长 → 缓存命中率下降 | 成本上升 | 分项开关；已实测缓存对前缀稳定，新增块在**历史之后**，不影响前缀命中 |
| 新标记干扰模型 | 误判加剧 | `grader.py` 离线评测把关；灰度逐项开 |
| observer 解析被打断 | 观测面板失效 | 单元测试断言 `_SPEECH_RE` 兼容；标记一律放 `[` 之前 |
| ContextVar 跨任务丢失 | 标记不生效 | `asyncio.create_task` 会复制 Context；单测覆盖 |
| 群聊历史条数提升 → 成本 | 上升 | 与 L6 预算项一起开关 |

**回滚**：所有能力均为**纯增量**（新增块 + 前缀标记），
关闭对应开关即恢复原样，**无需回退代码**。

---

## 11. 实施计划

| 阶段 | 内容 | 预估 | 产出 |
|---|---|---|---|
| **S0** | 基线测量：跑 9.3 的改造前基线 | 0.5d | 准确率基线数字 |
| **S1** | `group_context.py` + 单元测试 + L1/L2/L3 | 1.5d | 标记渲染正确 |
| **S2** | L4 触发原因透传（`message_service` → `run_agent`） | 0.5d | trigger_reason 可用 |
| **S3** | L5 契约文本（双语模板） | 0.5d | 判定规则落地 |
| **S4** | 9.3 离线评测改造后结果 | 0.5d | 前后对比数据 |
| **S5** | L6 预算调整 + 线上灰度 | 0.5d | 灰度观察 |

**总计约 4 人日。**

> 与 `UPGRADE_PLAN.md` 的关系：3.2（子 Agent）与上下文压缩的收益评估，
> 都依赖「模型是否理解了指向」这一前提。本设计若不落地，
> 压缩上下文会**更快地丢失指向信息**，反而加剧回错人。

---

## 12. 待评审确认项

1. **`AI_GROUP_CONTEXT_LENGTH_PER_SESSION` 提到 8192 是否可接受？**
   群聊历史从 5,120 → 8,192，prompt 增长约 60%，缓存命中率的实际影响待测。
2. **`trigger_reason=random` 时模型的默认行为**
   建议「默认不接话」，但这会让 Agent 在群里显得迟钝。是否给个可配置的「随机接话概率」？
3. **`reply_to=<id>` 用 ID 还是昵称？**
   建议 ID（唯一，机器可解析），昵称交给名册换算。是否需要同时给昵称？
4. **是否要加「未回复到 bot 时也显式标注 `not_to_me`」？**
   标记更明确，但每条都加会增加噪音与 token。当前设计只在**确定指向 bot** 时才加正向标记。
5. **历史顺序是否需要按「轮次」分组？**
   群聊里「一问一答」的多人交替很常见，当前是纯时间平铺。是否要按 reply 关系聚类？
6. **`mentions_me`（正文提到 bot 名字）要不要单列？**
   目前只有 `name_mentioned` 这个 trigger_reason，没有逐条标记。

---

## 附录 A · 关键代码位置索引

| 主题 | 位置 |
|---|---|
| 历史行渲染 | `nekro_agent/models/db_chat_message.py:51-78` |
| `is_system` 判定 | `nekro_agent/models/db_chat_message.py:85-87` |
| At 段渲染 | `nekro_agent/models/db_chat_message.py:127-131` |
| `turn_identity` 生成 | `nekro_agent/services/agent/templates/history.py:487-506` |
| 历史渲染主流程 | `nekro_agent/services/agent/templates/history.py:506-704` |
| `ref_msg_set` 计算 | `nekro_agent/services/agent/templates/history.py:669-673` |
| 历史截断逻辑 | `nekro_agent/services/agent/templates/history.py:686-698` |
| `render_history_message` | `nekro_agent/services/agent/templates/compiler.py:133-152` |
| `run_agent` 入口 | `nekro_agent/services/agent/run_agent.py:51` |
| 触发判定 | `nekro_agent/services/message_service.py:333-341` |
| 防抖任务创建 | `nekro_agent/services/message_service.py:139` |
| onebot `msg_to_me` → 插入 At | `nekro_agent/adapters/onebot_v11/tools/convertor.py:1058-1068` |
| onebot `@all` → is_tome | `nekro_agent/adapters/onebot_v11/tools/onebot_util.py:41-46` |
| `@` 文本解析 | `nekro_agent/tools/at_parser.py:22-40` |
| 群聊契约现状 | `nekro_agent/services/agent/templates/j2/runtime_contract.j2:7` |
| CoT「哪些消息被回复」 | `nekro_agent/services/agent/templates/j2/runtime_contract.j2:27` |
| observer 历史正则（兼容约束） | `nekro-agent-plugins/agent_observer/__init__.py:185` |

## 附录 B · 实测数据来源

| 数据 | 位置 |
|---|---|
| 群聊渲染样本 | `nekro-wuwa/runtime/logs/prompts/chat_log_20261021_002333_495081.json` |
| 全部群聊 chat_log | `nekro-wuwa/runtime/logs/prompts/`（830 份） |
| 用量与缓存 | `nekro-wuwa/runtime/observer/usage.jsonl`（2,118 条） |
| 调度轨迹 | `nekro-wuwa/runtime/observer/agent_trace.jsonl`（1,777 事件） |
| Eval 框架 | `nekro_agent/services/eval/` |
| Eval 现有 fixture | `nekro_agent/services/eval/fixtures/`（31 份，6 类场景） |
