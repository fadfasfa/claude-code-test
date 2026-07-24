"""Overlay 进程组生命周期状态机。

本模块只编排已注入的 host、sidecar、context poller 与模板 loader；具体实现由
bootstrap 组装，模块本身不访问 infrastructure。
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil

from hextech.modules.data.overlay_source import prepare_shared_overlay_data
from hextech.modules.vision.events import write_inactive_overlay_event
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path
from hextech.interfaces.overlay.lifecycle import (
    ProcessFactory,
    ProcessLike,
    SidecarCleanupError,
    SidecarStartCancelled,
    start_host_process,
    start_sidecar_process,
    stop_process,
)
from hextech.interfaces.overlay.context import start_overlay_context_poller, write_missing_overlay_context

OVERLAY_HOST_VISIBILITY_STALE_SECONDS = 6.0
TEMPLATE_PREWARM_WAIT_TIMEOUT_SECONDS = 8.0
OVERLAY_WARM_STARTUP_BUDGET_SECONDS = 30.0
OVERLAY_COLD_STARTUP_BUDGET_SECONDS = 60.0
OVERLAY_CONTINUATION_SECONDS = 120.0
SIDECAR_ATTEMPT_TIMEOUT_SECONDS = 60.0
SIDECAR_RETRY_DELAYS_SECONDS = (1.0,)
SIDECAR_HEARTBEAT_STALE_SECONDS = 10.0


class _OverlayStartCancelled(RuntimeError):
    """内部控制流：当前启动 generation 已被新的 desired state 取代。"""


class OverlayRuntimeManager:
    """Supervisor 持有的游戏内显示运行态。

    这里刻意只做生命周期编排和状态汇总：host、Vision sidecar、context poller
    与模板 runtime cache 的 owner 都收敛到 Runtime Supervisor。桌面 UI 只提交
    desired state，不再直接启动这些进程。
    """

    def __init__(
        self,
        *,
        start_host_func: ProcessFactory = start_host_process,
        start_sidecar_func: Callable[..., ProcessLike] = start_sidecar_process,
        start_context_poller_func: Callable[[], Any] | None = start_overlay_context_poller,
        prepare_data_func: Callable[[], Mapping[str, Any]] = prepare_shared_overlay_data,
        write_inactive_func: Callable[[], Any] = write_inactive_overlay_event,
        load_template_runtime_func: Callable[..., Any] | None = None,
        visibility_status_file: str | Path | None = None,
        sidecar_status_file: str | Path | None = None,
        prewarm_wait_timeout_seconds: float = TEMPLATE_PREWARM_WAIT_TIMEOUT_SECONDS,
        retry_sleep_func: Callable[[float], Any] | None = None,
        pid_exists: Callable[[int], bool] = psutil.pid_exists,
        process_create_time: Callable[[int], float | None] | None = None,
        now_func: Callable[[], float] = time.time,
    ) -> None:
        self._start_host_func = start_host_func
        self._start_sidecar_func = start_sidecar_func
        self._start_context_poller_func = start_context_poller_func
        self._prepare_data_func = prepare_data_func
        self._write_inactive_func = write_inactive_func
        self._load_template_runtime_func = load_template_runtime_func
        self._visibility_status_file = Path(visibility_status_file) if visibility_status_file is not None else Path(overlay_runtime_state_path("game_overlay_visibility.v1.json"))
        self._sidecar_status_file = (
            Path(sidecar_status_file)
            if sidecar_status_file is not None
            else Path(overlay_runtime_state_path("game_overlay_sidecar_status.json"))
        )
        self._prewarm_wait_timeout_seconds = max(0.1, float(prewarm_wait_timeout_seconds))
        self._retry_sleep_func = retry_sleep_func
        self._pid_exists = pid_exists
        self._process_create_time = process_create_time or self._default_process_create_time
        self._now_func = now_func
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._prewarm_thread: threading.Thread | None = None
        self._generation = 0
        self._start_cancel = threading.Event()
        self.host_process: ProcessLike | None = None
        self.sidecar_process: ProcessLike | None = None
        self.context_poller: Any | None = None
        self.context_error = ""
        self.desired_enabled = False
        self.status = "stopped"
        self.phase = "idle"
        self.cache_status = "idle"
        self.cache_hit: bool | None = None
        self.cache_stats: dict[str, Any] = {}
        self.startup_seconds = 0.0
        self.startup_mode = "unknown"
        self.target_budget_seconds = OVERLAY_COLD_STARTUP_BUDGET_SECONDS
        self.startup_attempts: list[dict[str, Any]] = []
        self._startup_started_at = 0.0
        self._startup_finished_at = 0.0
        self._startup_hard_deadline = 0.0
        self._startup_session_claimed = False
        self.visible_reason = ""
        self.functional_status = "unknown"
        self.functional_reason = ""
        self.last_error = ""
        self.last_start_failure_kind = ""
        self.updated_at = time.time()
        self._sidecar_started_at = 0.0
        self._sidecar_liveness: dict[str, Any] = {"status": "unknown", "reason": "not_started"}

    def _mark(self, *, status: str | None = None, phase: str | None = None, error: str | None = None) -> None:
        if status is not None:
            self.status = status
        if phase is not None:
            self.phase = phase
        if error is not None:
            self.last_error = error
        self.updated_at = time.time()

    def _refresh_startup_budget_locked(self) -> None:
        if self.cache_hit is True:
            self.startup_mode = "warm"
        elif self.cache_hit is False:
            self.startup_mode = "cold"
        else:
            self.startup_mode = "unknown"
        self.target_budget_seconds = (
            OVERLAY_WARM_STARTUP_BUDGET_SECONDS
            if self.startup_mode == "warm"
            else OVERLAY_COLD_STARTUP_BUDGET_SECONDS
        )
        if self._startup_started_at > 0.0:
            self._startup_hard_deadline = (
                self._startup_started_at + self.target_budget_seconds + OVERLAY_CONTINUATION_SECONDS
            )

    def _begin_startup_session_locked(self, *, claimed: bool) -> None:
        self._startup_started_at = time.perf_counter()
        self._startup_finished_at = 0.0
        self._startup_session_claimed = claimed
        self.startup_attempts = []
        self._refresh_startup_budget_locked()

    def _startup_elapsed_locked(self) -> float:
        if self._startup_started_at <= 0.0:
            return 0.0
        end = self._startup_finished_at or time.perf_counter()
        return round(max(0.0, end - self._startup_started_at), 3)

    def _finish_startup_session_locked(self) -> None:
        if self._startup_started_at > 0.0 and self._startup_finished_at <= 0.0:
            self._startup_finished_at = time.perf_counter()
        self.startup_seconds = self._startup_elapsed_locked()

    @staticmethod
    def _process_running(process: ProcessLike | None) -> bool:
        return bool(process is not None and process.poll() is None)

    @staticmethod
    def _default_process_create_time(pid: int) -> float | None:
        try:
            return float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError):
            return None

    def _host_pid(self) -> int | None:
        return getattr(self.host_process, "_hextech_overlay_runtime_pid", None) or getattr(self.host_process, "pid", None)

    def _sidecar_pid(self) -> int | None:
        return getattr(self.sidecar_process, "pid", None)

    def _read_sidecar_liveness(self) -> dict[str, Any]:
        """同时验证进程、PID 创建时间和 heartbeat，避免假 running。"""

        pid = self._sidecar_pid()
        now = self._now_func()
        startup_grace_active = bool(
            self._sidecar_started_at and now - self._sidecar_started_at <= SIDECAR_HEARTBEAT_STALE_SECONDS
        )
        if not self._process_running(self.sidecar_process) or not isinstance(pid, int) or pid <= 0:
            return {"status": "failed", "reason": "process_exited", "pid": pid}
        try:
            payload = json.loads(self._sidecar_status_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            if startup_grace_active:
                return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
            return {"status": "stale", "reason": "status_missing", "pid": pid}
        if not isinstance(payload, Mapping):
            if startup_grace_active:
                return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
            return {"status": "stale", "reason": "status_invalid", "pid": pid}
        try:
            schema_version = int(payload.get("schema_version") or 1)
            status_pid = int(payload.get("pid") or 0)
            heartbeat_at = float(payload.get("heartbeat_at") or payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            if startup_grace_active:
                return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
            return {"status": "stale", "reason": "status_invalid", "pid": pid}
        if status_pid != pid:
            # 新进程接管时，上一次 sidecar 的状态文件可能还在磁盘上；在首个
            # heartbeat 截止前把它当作 pending，而不是把刚启动的真实进程误判 stale。
            if startup_grace_active:
                return {
                    "status": "starting",
                    "reason": "heartbeat_pending",
                    "pid": pid,
                    "reported_pid": status_pid,
                }
            return {"status": "stale", "reason": "pid_mismatch", "pid": pid, "reported_pid": status_pid}
        try:
            exists = bool(self._pid_exists(pid))
        except Exception:
            exists = False
        if not exists:
            return {"status": "stale", "reason": "pid_missing", "pid": pid}
        if schema_version >= 2:
            try:
                reported_started_at = float(payload.get("pid_started_at") or 0.0)
            except (TypeError, ValueError):
                reported_started_at = 0.0
            actual_started_at = self._process_create_time(pid)
            if reported_started_at <= 0.0 or actual_started_at is None:
                return {"status": "stale", "reason": "pid_start_unavailable", "pid": pid}
            if abs(float(actual_started_at) - reported_started_at) > 2.0:
                return {
                    "status": "stale",
                    "reason": "pid_reused",
                    "pid": pid,
                    "pid_started_at": reported_started_at,
                    "actual_pid_started_at": actual_started_at,
                }
        if heartbeat_at <= 0.0 or now - heartbeat_at > SIDECAR_HEARTBEAT_STALE_SECONDS:
            return {
                "status": "stale",
                "reason": "heartbeat_stale",
                "pid": pid,
                "heartbeat_at": heartbeat_at,
            }
        return {
            "status": "running",
            "reason": "",
            "pid": pid,
            "heartbeat_at": heartbeat_at,
            "generation": str(payload.get("generation") or ""),
            "schema_version": schema_version,
        }

    def _sidecar_is_reusable(self) -> bool:
        liveness = self._read_sidecar_liveness()
        self._sidecar_liveness = liveness
        return liveness.get("status") in {"running", "starting"}

    def _mark_sidecar_stale_locked(self) -> None:
        liveness = self._read_sidecar_liveness()
        self._sidecar_liveness = liveness
        if liveness.get("status") not in {"running", "starting"}:
            # 无论是 PID 已退出、被复用还是 heartbeat 停止，先收敛为 stale。
            # 这样 Desktop 不会把一个还残留着 ProcessLike 对象的 sidecar 当成可用；
            # 下一次 enable 再沿用既有的启动重试/退避路径恢复。
            self._mark(
                status="stale",
                phase="sidecar_stale",
                error=f"Vision sidecar 存活失效：{liveness.get('reason') or 'unknown'}",
            )

    def _start_context_poller(self) -> None:
        if self.context_poller is not None or self._start_context_poller_func is None:
            return
        try:
            self.context_poller = self._start_context_poller_func()
            self.context_error = ""
        except Exception as exc:
            self.context_poller = None
            self.context_error = str(exc)

    def _stop_context_poller(self) -> None:
        poller = self.context_poller
        self.context_poller = None
        self.context_error = ""
        if poller is None:
            return
        stop = getattr(poller, "stop", None)
        if callable(stop):
            stop()
            return
        if callable(poller):
            poller()

    def _context_status(self) -> str:
        if self.context_poller is not None:
            return "running"
        if str(self.context_error or "").strip() in {"context_missing", "context_unavailable"}:
            return "context_missing"
        return "degraded" if self.context_error else "stopped"

    def _read_visibility_health(self) -> dict[str, str]:
        try:
            payload = json.loads(self._visibility_status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"visible_reason": "", "functional_status": "unknown", "functional_reason": ""}
        if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) not in {1, 2}:
            return {"visible_reason": "", "functional_status": "unknown", "functional_reason": "unknown_schema"}
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0.0 or time.time() - updated_at > OVERLAY_HOST_VISIBILITY_STALE_SECONDS:
            return {
                "visible_reason": "",
                "functional_status": "failed" if self._process_running(self.host_process) else "unknown",
                "functional_reason": "host_heartbeat_stale",
            }
        raw_decision = payload.get("decision")
        decision: Mapping[str, Any] = raw_decision if isinstance(raw_decision, Mapping) else {}
        schema_version = int(payload.get("schema_version") or 1)
        return {
            "visible_reason": str(decision.get("reason") or "").strip(),
            "functional_status": (
                str(payload.get("functional_status") or "unknown").strip()
                if schema_version >= 2
                else "ready"
            ),
            "functional_reason": str(payload.get("functional_reason") or "").strip(),
        }

    def _template_loader(self) -> Callable[..., Any]:
        if self._load_template_runtime_func is not None:
            return self._load_template_runtime_func
        raise RuntimeError("template runtime loader 未由 bootstrap 注入")

    def start_template_prewarm(self) -> None:
        with self._lock:
            if self._prewarm_thread is not None and self._prewarm_thread.is_alive():
                return
            if not self.desired_enabled:
                # 首轮预热属于桌面启动关键路径；首次 enable 必须认领同一会话。
                self._begin_startup_session_locked(claimed=False)
            self.cache_status = "queued"
            self.phase = "prewarming" if not self.desired_enabled else self.phase
            self.cache_hit = None
            self.cache_stats = {}
        thread = threading.Thread(target=self._prewarm_templates, name="hextech-overlay-template-prewarm", daemon=True)
        self._prewarm_thread = thread
        thread.start()

    def _prewarm_templates(self) -> None:
        started_at = time.perf_counter()
        try:
            with self._lock:
                self.cache_status = "prewarming"
            hint_cache = dict(self._prepare_data_func() or {})

            def _cache_status(phase: str, fields: Mapping[str, Any]) -> None:
                with self._lock:
                    self.phase = "prewarming" if not self.desired_enabled else self.phase
                    if phase == "template_runtime_cache_lookup":
                        self.cache_status = "lookup"
                    elif phase in {"template_index_build", "rank_matrix_build"}:
                        self.cache_status = "building"
                    else:
                        self.cache_status = str(phase or "prewarming")
                    if "cache_hit" in fields:
                        self.cache_hit = bool(fields.get("cache_hit"))
                        self._refresh_startup_budget_locked()
                    self.cache_stats = dict(fields)
                    self.startup_seconds = round(time.perf_counter() - started_at, 3)

            runtime = self._template_loader()(hint_cache=hint_cache, status_callback=_cache_status)
            with self._lock:
                self.cache_status = "ready"
                if self.cache_hit is not False:
                    self.cache_hit = True
                stats = getattr(runtime, "stats", None)
                if isinstance(stats, Mapping):
                    self.cache_stats = dict(stats)
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
                if not self.desired_enabled and self.status == "stopped":
                    self.phase = "ready"
                self.last_error = ""
        except Exception as exc:
            with self._lock:
                self.cache_status = "error"
                self.last_error = str(exc)
                self.startup_seconds = round(time.perf_counter() - started_at, 3)

    def _raise_template_prewarm_error_if_any(self) -> None:
        if self.cache_status == "error":
            raise RuntimeError(f"template runtime cache 预热失败：{self.last_error or 'unknown'}")

    def _ensure_start_current(self, generation: int, cancel_event: threading.Event) -> None:
        with self._lock:
            current = self.desired_enabled and generation == self._generation and not cancel_event.is_set()
        if not current:
            raise _OverlayStartCancelled("overlay 启动已取消")

    def _wait_for_template_prewarm(
        self,
        *,
        started_at: float,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        with self._lock:
            self._raise_template_prewarm_error_if_any()
            thread = self._prewarm_thread if self._prewarm_thread is not None and self._prewarm_thread.is_alive() else None
            if thread is not None:
                self.phase = "cache_wait"
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
        if thread is None:
            return
        while thread.is_alive():
            self._ensure_start_current(generation, cancel_event)
            with self._lock:
                hard_deadline = self._startup_hard_deadline
                elapsed = self._startup_elapsed_locked()
                target_budget = self.target_budget_seconds
            if hard_deadline > 0.0 and time.perf_counter() >= hard_deadline:
                with self._lock:
                    self.phase = "hard_timeout"
                    self.last_error = "Overlay 启动达到硬截止，模板预热仍未完成"
                    self.last_start_failure_kind = "hard_timeout"
                    self.startup_seconds = self._startup_elapsed_locked()
                raise TimeoutError(self.last_error)
            thread.join(timeout=min(0.1, self._prewarm_wait_timeout_seconds))
            with self._lock:
                self.phase = "vision_prewarming" if elapsed >= target_budget else "cache_wait"
                self.startup_seconds = self._startup_elapsed_locked()
        with self._lock:
            self._raise_template_prewarm_error_if_any()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if not bool(enabled):
            self.cancel_start("disabled")
            return self._stop("disabled")
        with self._operation_lock:
            with self._lock:
                if self._process_running(self.host_process) and self._sidecar_is_reusable():
                    self._start_context_poller()
                    self._mark(status="running", phase="running", error="")
                    return self.snapshot()
                if self._process_running(self.sidecar_process):
                    self._mark_sidecar_stale_locked()
                    if not stop_process(self.sidecar_process):
                        self._mark(status="error", phase="sidecar_stale_cleanup_failed", error="Vision sidecar stale 后无法停止")
                        return self.snapshot()
                    self.sidecar_process = None
                self._generation += 1
                generation = self._generation
                self._start_cancel.set()
                self._start_cancel = threading.Event()
                cancel_event = self._start_cancel
                self.desired_enabled = True
                now = time.perf_counter()
                can_claim_prewarm = bool(
                    not self._startup_session_claimed
                    and self._startup_started_at > 0.0
                    and self._startup_finished_at <= 0.0
                    and now < self._startup_hard_deadline
                )
                if can_claim_prewarm:
                    self._startup_session_claimed = True
                else:
                    self._begin_startup_session_locked(claimed=True)
            return self._start(generation, cancel_event)

    def cancel_start(self, reason: str = "cancelled") -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            self.desired_enabled = False
            self._start_cancel.set()
            if self.status == "starting":
                self._mark(status="stopping", phase=str(reason or "cancelled"))
            return self.snapshot()

    def _start(self, generation: int, cancel_event: threading.Event) -> dict[str, Any]:
        started_at = time.perf_counter()
        with self._lock:
            if self._process_running(self.host_process) and self._sidecar_is_reusable():
                self._start_context_poller()
                self._mark(status="running", phase="running", error="")
                self.last_start_failure_kind = ""
                return self.snapshot()
            self._mark(
                status="starting",
                phase="prepare_data",
                error=None if self.cache_status == "error" else "",
            )
        try:
            self._prepare_data_func()
            self._ensure_start_current(generation, cancel_event)
            self._write_inactive_func()
            with self._lock:
                self.phase = "context_start"
            self._start_context_poller()
            self._ensure_start_current(generation, cancel_event)
            with self._lock:
                self.phase = "host_start"
            if not self._process_running(self.host_process):
                self.host_process = self._start_host_func()
            if not self._process_running(self.host_process):
                raise RuntimeError("game_overlay host 启动后立即退出")
            self._ensure_start_current(generation, cancel_event)
            self._wait_for_template_prewarm(
                started_at=started_at,
                generation=generation,
                cancel_event=cancel_event,
            )
            with self._lock:
                self.phase = "sidecar_start"
            if not self._process_running(self.sidecar_process):
                self.sidecar_process = self._start_sidecar_with_retry(generation, cancel_event)
                self._sidecar_started_at = self._now_func()
            if not self._process_running(self.sidecar_process):
                raise RuntimeError("game_overlay sidecar 启动后立即退出")
            self._ensure_start_current(generation, cancel_event)
            with self._lock:
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
                self._finish_startup_session_locked()
                self._mark(status="running", phase="running", error="")
                self.last_start_failure_kind = ""
            return self.snapshot()
        except _OverlayStartCancelled:
            return self.snapshot()
        except Exception as exc:
            self._rollback_failed_start(str(exc))
            raise

    @staticmethod
    def _sidecar_failure_is_retryable(exc: Exception) -> bool:
        retryable = getattr(exc, "retryable", None)
        if retryable is not None:
            return bool(retryable)
        if isinstance(exc, (OSError, ValueError)):
            return False
        text = str(exc or "").casefold()
        deterministic_markers = (
            "template_missing",
            "模板缺失",
            "schema",
            "配置",
            "readiness token 不匹配",
            "硬截止",
        )
        return not any(marker.casefold() in text for marker in deterministic_markers)

    def _wait_before_sidecar_retry(
        self,
        delay_seconds: float,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        if self._retry_sleep_func is None:
            cancel_event.wait(timeout=delay_seconds)
        else:
            self._retry_sleep_func(delay_seconds)
        self._ensure_start_current(generation, cancel_event)

    def _start_sidecar_with_retry(
        self,
        generation: int,
        cancel_event: threading.Event,
    ) -> ProcessLike:
        for attempt in range(len(SIDECAR_RETRY_DELAYS_SECONDS) + 1):
            self._ensure_start_current(generation, cancel_event)
            with self._lock:
                remaining = self._startup_hard_deadline - time.perf_counter()
                if remaining <= 0.0:
                    self.last_start_failure_kind = "hard_timeout"
                    raise TimeoutError("Overlay 启动达到硬截止，sidecar 尚未就绪")
                timeout_seconds = min(SIDECAR_ATTEMPT_TIMEOUT_SECONDS, remaining)
                record = {
                    "attempt": attempt + 1,
                    "started_elapsed_seconds": self._startup_elapsed_locked(),
                    "readiness_timeout_seconds": round(timeout_seconds, 3),
                    "status": "starting",
                }
                self.startup_attempts.append(record)
            try:
                parameters = inspect.signature(self._start_sidecar_func).parameters
                accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
                if accepts_kwargs or {"readiness_timeout_seconds", "cancel_event"}.issubset(parameters):
                    process = self._start_sidecar_func(
                        readiness_timeout_seconds=timeout_seconds,
                        cancel_event=cancel_event,
                    )
                else:
                    process = self._start_sidecar_func()
                try:
                    self._ensure_start_current(generation, cancel_event)
                except _OverlayStartCancelled:
                    if not stop_process(process):
                        raise SidecarCleanupError(
                            f"Vision sidecar 启动取消后进程清理失败(pid={getattr(process, 'pid', None)})"
                        )
                    raise
                with self._lock:
                    if time.perf_counter() >= self._startup_hard_deadline:
                        self.last_start_failure_kind = "hard_timeout"
                        if not stop_process(process):
                            raise SidecarCleanupError(
                                f"Vision sidecar 超过硬截止后进程清理失败(pid={getattr(process, 'pid', None)})"
                            )
                        raise TimeoutError("Overlay 启动达到硬截止，sidecar readiness 迟到")
                    record["status"] = "ready"
                    record["completed_elapsed_seconds"] = self._startup_elapsed_locked()
                return process
            except SidecarStartCancelled as exc:
                with self._lock:
                    record["status"] = "cancelled"
                    record["error_type"] = exc.__class__.__name__
                    record["completed_elapsed_seconds"] = self._startup_elapsed_locked()
                raise _OverlayStartCancelled(str(exc)) from exc
            except Exception as exc:
                retryable = self._sidecar_failure_is_retryable(exc)
                with self._lock:
                    record["status"] = "failed"
                    record["error_type"] = exc.__class__.__name__
                    record["retryable"] = retryable
                    record["completed_elapsed_seconds"] = self._startup_elapsed_locked()
                if attempt >= len(SIDECAR_RETRY_DELAYS_SECONDS) or not retryable:
                    raise
                self._wait_before_sidecar_retry(
                    SIDECAR_RETRY_DELAYS_SECONDS[attempt],
                    generation,
                    cancel_event,
                )
        raise RuntimeError("Vision sidecar 启动重试状态异常")

    def _rollback_failed_start(self, reason: str) -> None:
        errors: list[str] = []
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"inactive 事件写入失败：{exc}")
        try:
            self._stop_context_poller()
        except Exception as exc:
            errors.append(f"context poller 停止失败：{exc}")
        if not stop_process(self.sidecar_process):
            errors.append(f"sidecar 停止失败(pid={self._sidecar_pid()})")
        else:
            self.sidecar_process = None
        if not stop_process(self.host_process):
            errors.append(f"host 停止失败(pid={self._host_pid()})")
        else:
            self.host_process = None
        error = "；".join([reason, *errors]) if errors else reason
        with self._lock:
            self._finish_startup_session_locked()
            self._mark(status="error", phase="failed", error=error)
            if self.last_start_failure_kind != "hard_timeout":
                self.last_start_failure_kind = self._classify_start_failure_kind(reason)

    @staticmethod
    def _classify_start_failure_kind(reason: str) -> str:
        text = str(reason or "")
        if "game_overlay host 启动超时" in text:
            return "host_readiness_timeout"
        if "Vision sidecar" in text or "game_overlay sidecar" in text:
            return "sidecar_failed"
        if "readiness token 不匹配" in text:
            return "host_readiness_token_mismatch"
        if "game_overlay host 在 readiness 前退出" in text or "host 启动后立即退出" in text:
            return "host_exited"
        return "start_failed"

    def _stop(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self.desired_enabled = False
            self._mark(status="stopping", phase=reason)
        errors: list[str] = []
        try:
            self._stop_context_poller()
        except Exception as exc:
            errors.append(f"context poller 停止失败：{exc}")
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"inactive 事件写入失败：{exc}")
        try:
            write_missing_overlay_context(source=f"supervisor-{reason}")
        except Exception as exc:
            errors.append(f"空上下文写入失败：{exc}")
        sidecar_stopped = stop_process(self.sidecar_process)
        # sidecar graceful exit 期间可能最后写出一帧 active 事件；停止后再写一次
        # inactive，确保 host/renderer 最终看到隐藏态。
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"最终 inactive 事件写入失败：{exc}")
        host_stopped = stop_process(self.host_process)
        if sidecar_stopped:
            self.sidecar_process = None
        else:
            errors.append(f"sidecar 停止失败(pid={self._sidecar_pid()})")
        if host_stopped:
            self.host_process = None
        else:
            errors.append(f"host 停止失败(pid={self._host_pid()})")
        with self._lock:
            self._finish_startup_session_locked()
            if errors:
                self._mark(status="error", phase="stop_failed", error="；".join(errors))
            else:
                self._mark(status="stopped", phase="stopped", error="")
                self.last_start_failure_kind = ""
        if errors:
            raise RuntimeError("；".join(errors))
        return self.snapshot()

    def shutdown(self, reason: str = "shutdown") -> None:
        shutdown_reason = str(reason or "shutdown")
        self.cancel_start(shutdown_reason)
        self._stop(shutdown_reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self.status == "running":
                if not self._process_running(self.host_process):
                    self._mark(status="error", phase="host_exited", error="game_overlay host 意外退出")
                elif self.sidecar_process is not None:
                    # Process.poll() 只能说明句柄尚未退出，无法识别 PID 复用和
                    # sidecar 卡死。每次状态快照都检查小型 status 文件的心跳。
                    self._mark_sidecar_stale_locked()
            elif self.sidecar_process is not None:
                self._sidecar_liveness = self._read_sidecar_liveness()
            visibility_health = self._read_visibility_health()
            self.visible_reason = visibility_health["visible_reason"]
            self.functional_status = visibility_health["functional_status"]
            self.functional_reason = visibility_health["functional_reason"]
            self._refresh_startup_budget_locked()
            startup_elapsed = self._startup_elapsed_locked()
            fallback_recommended = bool(
                self.desired_enabled
                and self.status in {"starting", "error"}
                and (self.status == "error" or startup_elapsed >= self.target_budget_seconds)
            )
            hard_timeout_reached = bool(
                self.last_start_failure_kind == "hard_timeout"
                or (
                    self.desired_enabled
                    and startup_elapsed >= self.target_budget_seconds + OVERLAY_CONTINUATION_SECONDS
                )
            )
            return {
                "desired_enabled": self.desired_enabled,
                "status": self.status,
                "phase": self.phase,
                "host_pid": self._host_pid() if self._process_running(self.host_process) else None,
                "sidecar_pid": self._sidecar_pid() if self._process_running(self.sidecar_process) else None,
                "sidecar_liveness": dict(self._sidecar_liveness),
                "context_status": self._context_status(),
                "cache_status": self.cache_status,
                "cache_hit": self.cache_hit,
                "cache_stats": dict(self.cache_stats),
                "startup_seconds": self.startup_seconds,
                "startup_mode": self.startup_mode,
                "startup_elapsed_seconds": startup_elapsed,
                "target_budget_seconds": self.target_budget_seconds,
                "fallback_recommended": fallback_recommended,
                "hard_timeout_reached": hard_timeout_reached,
                "startup_attempts": [dict(attempt) for attempt in self.startup_attempts],
                "visible_reason": self.visible_reason,
                "functional_status": self.functional_status,
                "functional_reason": self.functional_reason,
                "last_error": self.last_error,
                "last_start_failure_kind": self.last_start_failure_kind,
                "generation": self._generation,
                "updated_at": self.updated_at,
            }
