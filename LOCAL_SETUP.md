# 本地部署约定（Fork 说明）

本仓库是 [KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent) 的 fork，用于在本地部署上做架构升级实验。

## 分支约定

| 分支 | 用途 | 版本 |
|---|---|---|
| `main` | 上游镜像，不动 | 跟随上游 |
| `local/upgrade` | **本地改造分支**（实际部署用的就是它） | 基于 `v2.4.0` |

`local/upgrade` 从 tag **`v2.4.0`** 切出，因为生产运行的镜像是 `ghcr.io/kromiose/nekro-agent:latest`，其 `pyproject.toml` 版本号为 `2.4.0`。

> **为什么不用 `main`**：fork 时 `main` 已比 `v2.4.0` 多出 11 个提交（含正则命令触发、工作区授权拆分等未随镜像发布的变更）。直接部署 `main` 会引入镜像里没有的未测试代码，可能触发未执行的数据库迁移。

## 部署方式：源码挂载

不重建镜像，而是用 bind mount 把本仓库的 `nekro_agent/` 覆盖到容器内的 `/app/nekro_agent`。

`docker-compose.yml`（在 `nekro-wuwa/` 目录）中的配置：

```yaml
  agent:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - type: bind
        source: ./runtime
        target: /run/desktop/mnt/host/e/Desktop/Sigrika/nekro-wuwa/runtime
      # core 源码挂载
      - type: bind
        source: ../nekro-agent-core/nekro_agent
        target: /app/nekro_agent
```

### 目录关系

```
E:\Desktop\Sigrika\
├── nekro-agent-core\          ← 本仓库（git 管理）
│   └── nekro_agent\           ← 挂载到容器 /app/nekro_agent
└── nekro-wuwa\                ← 部署目录
    ├── docker-compose.yml
    └── runtime\               ← 数据目录（已挂载）
```

## 日常开发流程

```bash
# 1. 改代码（在 nekro-agent-core/ 里）
# 2. 重启进程（不是重建容器！）
docker restart nekro-wuwa-agent-1
# 3. 看日志确认
docker logs nekro-wuwa-agent-1 --since 2m | grep -E "插件加载|路由数量|Traceback"
```

**改动生效只需 `docker restart`**（Python 启动时 import 模块，改源文件不会热更新）。
**`docker compose up -d --force-recreate` 也不会丢改动**（代码在宿主机，不在容器可写层）。

## 上游同步

```bash
# 拉取上游更新
git fetch upstream --tags

# 查看新版本
git log --oneline v2.4.0..upstream/main

# 合并到本地分支（建议用 merge 保留上游提交历史）
git checkout local/upgrade
git merge upstream/main
```

**升级注意**：
1. 先看上游有没有新增 `migrations/`，有的话需要同步挂载或重建镜像
2. `pyproject.toml` 的依赖变更需要重建镜像才能生效（挂载只覆盖代码，不覆盖依赖）
3. 升级后核对 `pyproject.toml` 版本号与镜像版本是否一致

## 回滚

| 场景 | 操作 |
|---|---|
| 某次改动出问题 | `git revert <commit>` 或 `git checkout -- <file>`，然后 `docker restart` |
| 整体回滚到镜像原版 | 删掉 `docker-compose.yml` 里的 `nekro-agent-core` 挂载，`docker compose up -d --force-recreate` |
| 回滚到某个标签 | `git checkout v2.4.0`，`docker restart` |

## 验证基线

挂载生效后的健康指标（2026-10-20 实测）：

| 指标 | 值 |
|---|---|
| 插件加载 | 39 个，零初始化失败 |
| 路由 | agent_observer 4 / deepseek_thinking 3 / emotions_pro 30 / memory_angel 61 / persona_guard 2 |
| API | `GET /api/user/me` → 401（未登录，正常） |
| 容器 | `Up (healthy)` |

## 已知限制

- 挂载只覆盖 `nekro_agent/`。改 `pyproject.toml` / `migrations/` / `plugins/` 不会生效（需要重建镜像或额外挂载）
- 仓库配了 `core.autocrlf=false`（Windows 上必须），否则检出会把 LF 转成 CRLF，与 Linux 容器不兼容
- 上游 License 是 `NOASSERTION`（自定义），fork 自用没问题，二次分发前需读原文

## 代码质量门禁

仓库的 `AGENTS.md` 要求后端改动通过 `poe lint`。**但生产镜像用 `uv sync --no-dev` 构建，容器内没有 `poe` / `ruff` / `basedpyright`**，宿主机也未安装 `poe`。

实测可行的替代（宿主机执行，检查的是挂载进容器的同一份源码）：

```powershell
cd E:\Desktop\Sigrika\nekro-agent-core
uv tool run ruff check nekro_agent        # 等价于 poe lint
```

- 首次运行会自动下载 ruff（约 5 秒），之后秒出
- **基线已验证干净**：v2.4.0 上跑出 `All checks passed!`（exit 0）
- 若需要 `poe` 的其它任务（format / typecheck / test），需先 `uv sync --all-extras` 装开发依赖
- 前端（`frontend/`）的 `poe frontend-check` 需要 pnpm + node，当前部署未使用前端源码，跳过
