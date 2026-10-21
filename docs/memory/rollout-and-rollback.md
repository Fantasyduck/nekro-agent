# 按频道灰度与回滚

本页记录第九阶段（上线验收）所需的可执行能力：发布状态机怎么落地、预检看什么、
切换与回滚各要做什么。设计依据见 `outputs/nekro-memory-detailed-design-v1.md` 的
§17.1（发布状态）、§17.6（按频道灰度与单一事实所有权）、§17.7（回滚步骤）。

## 1. 状态机与代码的对应

| 设计状态 | `mem_scope.pipeline_mode` | 事实读写所有权 |
| --- | --- | --- |
| LegacyGuarded | `legacy_guarded` | 旧实现（受保护适配器）；新候选不发布 |
| Shadow | `shadow` | 旧实现写入；新管线只比对，结果进 `shadow_result`，不进 prompt |
| Canary | `service` | 统一服务同时持有读写；旧写入口被 `LEGACY_WRITER_DISABLED` 拒绝 |
| Unified | 全部目标频道 `service` | 同上 |

关键点：**没有独立的「读开关」和「写开关」**。`pipeline_mode` 是单一真值来源，
`get_memory_route` 读它决定核心装配走 service 还是旧路径，`legacy_fact_write` 读它拒绝旧写入。
所以「切读不切写」这种半吊子状态在代码里无法表达，这正是设计要求的单一事实所有权。

`writer_revision` 是 fencing token：`set_mode` 每次真正改变模式就 +1，
持有旧版本的抽取任务在 `complete`/`fail` 时会被拒绝，不会用旧模式的结果覆盖新状态。

## 2. 切换前预检

`GET /memory_facts/rollout?workspace_id=&chat_key=` 返回 `RolloutReport`：

| 字段 | 含义 |
| --- | --- |
| `mode` / `writer_revision` | 当前模式与版本，切换时必须原样回传 |
| `pending_jobs` | 待领取的抽取任务数 |
| `running_jobs` | 仍持有租约的在途任务数 |
| `index_pending` / `index_dead_letter` | 索引待处理与死信数 |
| `active_assertions` | 当前生效事实数 |
| `ready` / `blockers` | 是否可以切换、阻断项代码 |
| `checks` | 每项检查的 `code`/`ok`/`blocker`/`detail` |

阻断规则只有一条是硬的：**有在途任务就不许切换**。原因很直接——新旧两侧同时写同一逻辑键，
会产生两个都自称当前的版本。其余三项（待领取、索引积压、索引死信）只提示不阻断：
它们在切换后仍能按新版本推进，但运维应当在观察窗口内确认它们确实在下降。

`active_assertions == 0` 也不阻断，只是提醒：先确认抽取本身是否正常，否则切过去也是空库。

## 3. 切换

`POST /memory_facts/rollout`：

```json
{
  "workspace_id": 1,
  "chat_key": "onebot_v11-group_884011695",
  "mode": "service",
  "expected_writer_revision": 3,
  "isolate_leases": false,
  "reason": "canary-2026-10-22"
}
```

- `expected_writer_revision` 不符直接 `WRITER_FENCED`（409），避免两个运维动作互相覆盖；
- 已有在途任务且 `isolate_leases=false` → `ROLLOUT_BUSY`（409），要求先处置；
- `isolate_leases=true`（仅回滚用）会把持有租约的任务退回 `pending`、清空 `lease_token`、
  标记 `error_class="LEASE_ISOLATED"`，旧所有者下次提交因 `writer_revision` 不符被拒；
- 模式未变时幂等返回当前状态，**不抬版本**；
- 每次真实切换写审计 `pipeline.<mode>`，隔离租约写 `pipeline.leases_isolated`。

切换成功返回的是切换**之后**重新预检的报告，调用方不需要再查一次。

## 4. 回滚

按设计 §17.7 的顺序，代码侧可执行的只有第 4 步的后半段，前半段（追平兼容投影）属于运维动作：

1. 暂停扩大灰度，记录频道、配置版本与最后提交水位；
2. 影子阶段直接停影子消费即可，线上仍是 LegacyGuarded；
3. 已进入 `service` 的频道：先停止新任务领取，再 `POST /rollout` 回 `legacy_guarded` 并带
   `isolate_leases=true`，把在途租约隔离掉；
4. 用 `GET /memory_facts/rollout` 与迁移对账确认批次已追平，然后才允许旧路径对外服务；
5. 保留新表、`mem_legacy_map`、tombstone、撤权与审计，**不删表回退 schema**；
6. 恢复任务消费，验证一轮新事实、纠错与删除。

**不能**恢复旧备份后直接对外：备份可能缺少最近的删除与撤权记录。
`forget` 是逻辑删除并在读路径立即阻断，旧备份回滚会让已遗忘的事实复活。

## 5. 测试

`tests/test_memory_rollout.py`（6 项）：

- 有租约在途时 `ready=false` 且阻断项含 `in_flight_jobs`；
- 在途时切换被 `ROLLOUT_BUSY` 拒绝；显式隔离后成功，任务转 `pending`、租约清空、
  审计 `pipeline.leases_isolated` 与 `pipeline.service` 齐备；
- `expected_writer_revision` 不符报 `WRITER_FENCED`；同模式重复切换不抬版本；
- 非管理员、非 `admin_review` 被拒；未知模式报 `PIPELINE_MODE_INVALID`；
- 待领取任务只提示不阻断，切换后仍能完成；
- 切到 `service` 后 `legacy_fact_write` 抛 `LEGACY_WRITER_DISABLED`，切回 `shadow` 后恢复
  ——这是回滚路径的关键断言。

全量回归：461 passed / 2 skipped / 2 failed（失败仍是既有的 `test_plugin_call_priority`
与 `test_tool_result_continuation`）。

## 6. 还没做的部分

- **兼容投影追平**：回滚第 4 步要求把灰度期间的事实更新、删除、授权变更重放到旧兼容投影。
  目前 `legacy_fact_write` 只是拒绝写入，并没有「从权威事实反向生成旧表投影」的单向同步器；
- **备份与恢复演练**：设计 §17.2 要求 PostgreSQL 一致性备份 + SQLite backup API，
  并验证「先重放控制记录再开放读取」；
- **合成评测与 p95／费用观测**：Canary 转 Unified 的前置证据，属于运营指标，代码未采。