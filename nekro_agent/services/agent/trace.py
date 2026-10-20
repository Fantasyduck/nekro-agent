"""Agent 运行轨迹（Trace）标准化采集。

背景：core 此前只有 `AgentRuntimeStatusEvent`（推给前端的状态广播），没有结构化的
运行轨迹。要评估 prompt 组装、工具目录化、compaction 等改造是否真的变好，
需要一个**离线可分析**的事件流，而不是事后去扫日志反推。

设计约束：
1. **零侵入**：所有埋点集中在 `run_agent.publish_runtime_state` 内部一处，
   不改动 agent 主循环的控制流；
2. **零成本**：关闭时（默认）只是一个布尔判断，不分配对象、不加锁；
3. **有界**：内存环形缓冲，天然防内存泄漏；可选落盘为 JSONL。
4. **可分析**：事件字段固定，前端面板与离线 eval 消费同一份 schema。
"""

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("agent_trace")

# 事件类型：与 AgentRuntimeStatusEvent 的 phase 对齐，另加生命周期类事件
PHASE_LLM_GENERATING = "llm_generating"
PHASE_LLM_RETRYING = "llm_retrying"
PHASE_SANDBOX_RUNNING = "sandbox_running"
PHASE_SANDBOX_STOPPED = "sandbox_stopped"
PHASE_ITERATING = "iterating"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"

EVENT_PROMPT_BUILT = "prompt_built"
EVENT_LLM_REQUEST = "llm_request"
EVENT_SANDBOX_RESULT = "sandbox_result"


@dataclass
class TraceEvent:
    """单条轨迹事件。

    字段刻意保持扁平且可 JSON 序列化，便于直接落盘与离线分析。
    """

    seq: int = 0
    ts: float = 0.0
    ts_ms: int = 0
    event: str = ""
    chat_key: str = ""
    phase: Optional[str] = None
    iteration_index: int = 1
    iteration_total: int = 1
    llm_retry_index: int = 1
    llm_retry_total: int = 1
    model_name: Optional[str] = None
    sandbox_stop_type: Optional[int] = None
    error_summary: Optional[str] = None
    # 额外负载（prompt 指标、token、工具信息等），按需填充
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TraceEmitter:
    """轨迹事件发射器。

    线程安全：agent 主循环在事件循环里跑，落盘线程是独立的，因此用锁保护缓冲。
    """

    def __init__(self) -> None:
        self._enabled = False
        self._buffer: Deque[TraceEvent] = deque(maxlen=2000)
        self._lock = threading.Lock()
        self._seq = 0
        self._path: Optional[Path] = None
        self._fh = None

    # ---------------------------------------------------------------- 开关

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, *, enabled: bool, path: Optional[str] = None, buffer_size: int = 2000) -> None:
        """配置开关与落盘路径。关闭时会主动释放文件句柄。"""
        with self._lock:
            self._enabled = bool(enabled)
            self._buffer = deque(self._buffer, maxlen=max(100, int(buffer_size)))
            if path:
                self._path = Path(path)
            if not self._enabled:
                self._close_file_locked()

    def _close_file_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    # ---------------------------------------------------------------- 发射

    def emit(self, event: str, chat_key: str = "", **kwargs: Any) -> Optional[TraceEvent]:
        """记录一条事件。未启用时立即返回 None（调用方零成本）。"""
        if not self._enabled:
            return None
        now = time.time()
        with self._lock:
            self._seq += 1
            payload = {k: v for k, v in kwargs.items() if k in _EVENT_FIELDS}
            extra = {k: v for k, v in kwargs.items() if k not in _EVENT_FIELDS}
            ev = TraceEvent(
                seq=self._seq,
                ts=now,
                ts_ms=int(now * 1000),
                event=event,
                chat_key=chat_key,
                phase=payload.get("phase"),
                iteration_index=int(payload.get("iteration_index") or 1),
                iteration_total=int(payload.get("iteration_total") or 1),
                llm_retry_index=int(payload.get("llm_retry_index") or 1),
                llm_retry_total=int(payload.get("llm_retry_total") or 1),
                model_name=payload.get("model_name"),
                sandbox_stop_type=payload.get("sandbox_stop_type"),
                error_summary=payload.get("error_summary"),
                data=extra,
            )
            self._buffer.append(ev)
            self._write_locked(ev)
        return ev

    def _write_locked(self, ev: TraceEvent) -> None:
        if not self._path:
            return
        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            self._fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"trace 落盘失败，已停用落盘: {type(e).__name__}: {e}")
            self._path = None
            self._close_file_locked()

    # ---------------------------------------------------------------- 读取

    def snapshot(self, chat_key: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """取最近的事件（可选按 chat_key 过滤）。"""
        with self._lock:
            items = list(self._buffer)
        if chat_key:
            items = [e for e in items if e.chat_key == chat_key]
        return [e.to_dict() for e in items[-max(1, int(limit)):]]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


# 事件字段白名单：其余 kwargs 归入 data
_EVENT_FIELDS = {
    "phase",
    "iteration_index",
    "iteration_total",
    "llm_retry_index",
    "llm_retry_total",
    "model_name",
    "sandbox_stop_type",
    "error_summary",
}


# 全局单例
trace_emitter = TraceEmitter()


def configure_trace(*, enabled: bool, path: Optional[str] = None, buffer_size: int = 2000) -> None:
    """按配置初始化轨迹采集（由 core 启动流程调用）。"""
    trace_emitter.configure(enabled=enabled, path=path, buffer_size=buffer_size)
    logger.info(f"Agent 轨迹采集：{'已启用' if enabled else '未启用'}" + (f"（落盘 {path}）" if enabled and path else ""))


def emit(event: str, chat_key: str = "", **kwargs: Any) -> Optional[TraceEvent]:
    """模块级便捷入口。"""
    return trace_emitter.emit(event, chat_key, **kwargs)
