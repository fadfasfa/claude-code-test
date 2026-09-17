"""DataService 的有界串行 action 队列与 loopback 控制面状态。"""

from __future__ import annotations

import queue
import secrets
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from hextech.bootstrap.game_refresh_gate import REFRESH_SCOPES, normalize_refresh_scope


DATA_SERVICE_NONCE_HEADER = "X-Hextech-Data-Service-Nonce"


class DataServiceApplication:
    """只绑定 loopback 的 DataService 控制面。"""

    def __init__(self, *, core: Any, parent_pid: int, nonce: str | None = None) -> None:
        self.core = core
        self.parent_pid = int(parent_pid)
        self.nonce = nonce or secrets.token_urlsafe(24)
        self.shutdown_requested = threading.Event()
        self._actions: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(maxsize=8)
        self._action_state_lock = threading.Lock()
        self._active_action: dict[str, Any] | None = None
        self._last_action: dict[str, Any] | None = None
        self._completed_actions: dict[str, dict[str, Any]] = {}
        self._queued_action_types: set[str] = set()
        self._pending_refresh_recheck = False
        self._pending_refresh_force = False
        self._pending_refresh_scope = "due"
        self._queued_refresh_scope = "due"
        threading.Thread(target=self._run_actions, name="hextech-data-actions", daemon=True).start()

    def submit_action(
        self,
        action_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提交有界串行 action；HTTP 线程绝不等待真实抓取完成。"""

        if action_type not in {"refresh", "set_private_stats"}:
            return {"accepted": False, "reason_code": "unsupported_action"}
        normalized_payload = dict(payload or {})
        if action_type == "refresh":
            raw_scope = str(normalized_payload.get("scope") or "due").strip().lower()
            if raw_scope not in REFRESH_SCOPES:
                return {"accepted": False, "reason_code": "invalid_refresh_scope"}
            normalized_payload["scope"] = raw_scope
            normalized_payload["force"] = bool(normalized_payload.get("force"))
        with self._action_state_lock:
            if self.shutdown_requested.is_set():
                return {"accepted": False, "reason_code": "shutdown_requested"}
            active_type = str((self._active_action or {}).get("type") or "")
            if action_type == "refresh" and (
                active_type == "refresh" or action_type in self._queued_action_types
            ):
                return self._coalesce_refresh_locked(normalized_payload)
            action_id = uuid.uuid4().hex
            try:
                self._actions.put_nowait((action_id, action_type, normalized_payload))
            except queue.Full:
                return {"accepted": False, "reason_code": "queue_full"}
            self._queued_action_types.add(action_type)
            if action_type == "refresh":
                self._queued_refresh_scope = str(normalized_payload.get("scope") or "due")
        return {"accepted": True, "action_id": action_id, "status": "queued"}

    def _coalesce_refresh_locked(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._pending_refresh_recheck = True
        pending_full_force = self._pending_refresh_force and self._pending_refresh_scope == "due"
        incoming_full_force = bool(payload.get("force")) and payload.get("scope") == "due"
        self._pending_refresh_force = self._pending_refresh_force or bool(payload.get("force"))
        if pending_full_force or incoming_full_force:
            self._pending_refresh_scope = "due"
        elif payload.get("scope") == "core":
            self._pending_refresh_scope = "core"
        return {
            "accepted": True,
            "reason_code": "pending_recheck",
            "status": "coalesced",
            "force": self._pending_refresh_force,
            "scope": self._pending_refresh_scope,
        }

    def _refresh_status_locked(self, core_status: Mapping[str, Any]) -> dict[str, Any]:
        """把 action 生命周期投影为 UI 可直接消费的稳定刷新状态。"""

        active = self._active_action or {}
        progress = core_status.get("executor_progress")
        if isinstance(progress, Mapping) and progress.get("state") != "idle" and (
            not active or float(progress.get("started_at") or 0) >= float(active.get("started_at") or 0)
        ):
            # 来源/阶段/计数只由执行器提供。旧测试适配器没有该接口时才走旧action状态。
            return {**dict(progress), "scope": normalize_refresh_scope(active.get("scope")),
                    "pending_sources": ([str(progress["source"])] if progress.get("source") and
                                        progress.get("state") in {"running", "core_ready"} else [])}
        if active.get("type") == "refresh":
            scope = normalize_refresh_scope(active.get("scope"))
            return _refresh_status_payload(
                state="running",
                scope=scope,
                phase="core",
                reason=(
                    "resumed_after_game"
                    if bool(active.get("resumed_after_game"))
                    else "refresh_running"
                ),
                generation_id=str(core_status.get("generation_id") or ""),
                pending_sources=["aramkit", "blitz"] if scope == "core" else [],
                started_at=float(active.get("started_at") or 0.0),
            )
        if "refresh" in self._queued_action_types:
            return _refresh_status_payload(
                state="queued",
                scope=self._queued_refresh_scope,
                phase="core",
                reason="refresh_queued",
                generation_id=str(core_status.get("generation_id") or ""),
                pending_sources=(
                    ["aramkit", "blitz"] if self._queued_refresh_scope == "core" else []
                ),
            )
        last_refresh = self._last_refresh_locked()
        if last_refresh is None:
            return _refresh_status_payload(
                state="idle",
                scope="due",
                phase="",
                reason="",
                generation_id=str(core_status.get("generation_id") or ""),
            )
        result_value = last_refresh.get("result")
        result = result_value if isinstance(result_value, Mapping) else {}
        reason = str(result.get("reason_code") or "")
        state = _terminal_refresh_state(result, reason)
        return _refresh_status_payload(
            state=state,
            scope=normalize_refresh_scope(result.get("refresh_scope")),
            phase=str(result.get("refresh_phase") or "complete"),
            reason=reason,
            generation_id=str(
                result.get("generation_id") or core_status.get("generation_id") or ""
            ),
            pending_sources=[str(item) for item in result.get("pending_sources") or []],
            started_at=float(last_refresh.get("started_at") or 0.0),
            completed_at=float(last_refresh.get("completed_at") or 0.0),
            checked_at=float(result.get("checked_at") or 0.0),
            data_at=str(result.get("data_at") or ""),
            checked=bool(result.get("checked")),
            content_changed=bool(result.get("content_changed")),
            catalog_changed=bool(result.get("catalog_changed")),
            last_good_available=bool(result.get("last_good_available")),
            source_outcomes=(
                dict(result.get("source_outcomes") or {})
                if isinstance(result.get("source_outcomes"), Mapping)
                else {}
            ),
            catalog_state=str(result.get("catalog_state") or ""),
        )

    def _last_refresh_locked(self) -> Mapping[str, Any] | None:
        if (self._last_action or {}).get("type") == "refresh":
            return self._last_action
        for action in reversed(tuple(self._completed_actions.values())):
            if action.get("type") == "refresh":
                return action
        return None

    def status(self) -> dict[str, Any]:
        status = self.core.status()
        with self._action_state_lock:
            status["active_action"] = dict(self._active_action) if self._active_action else None
            status["last_action"] = dict(self._last_action) if self._last_action else None
            status["actions"] = {key: dict(value) for key, value in self._completed_actions.items()}
            status["queued_action_count"] = self._actions.qsize()
            status["pending_refresh_recheck"] = self._pending_refresh_recheck
            status["pending_refresh_force"] = self._pending_refresh_force
            status["pending_refresh_scope"] = self._pending_refresh_scope
            status["refresh_status"] = self._refresh_status_locked(status)
        return status

    def request_shutdown(self) -> None:
        """停止接收 action；pending 只代表本进程后续工作，退出时必须丢弃。"""

        self.shutdown_requested.set()
        with self._action_state_lock:
            self._clear_pending_refresh_locked()
            self._queued_refresh_scope = "due"

    def _clear_pending_refresh_locked(self) -> None:
        self._pending_refresh_recheck = False
        self._pending_refresh_force = False
        self._pending_refresh_scope = "due"

    def _queue_pending_refresh_locked(self) -> None:
        if self.shutdown_requested.is_set():
            self._clear_pending_refresh_locked()
            self._queued_refresh_scope = "due"
            return
        if not self._pending_refresh_recheck or "refresh" in self._queued_action_types:
            return
        action_id = uuid.uuid4().hex
        force = self._pending_refresh_force
        scope = self._pending_refresh_scope
        try:
            self._actions.put_nowait(
                (action_id, "refresh", {"force": force, "scope": scope, "recheck": True})
            )
        except queue.Full:
            return
        self._clear_pending_refresh_locked()
        self._queued_action_types.add("refresh")
        self._queued_refresh_scope = scope

    def _run_actions(self) -> None:
        while not self.shutdown_requested.is_set():
            try:
                action_id, action_type, payload = self._actions.get(timeout=0.2)
            except queue.Empty:
                continue
            started_at = time.time()
            with self._action_state_lock:
                self._queued_action_types.discard(action_type)
                if action_type == "refresh":
                    self._queued_refresh_scope = "due"
                self._active_action = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": "running",
                    "started_at": started_at,
                    "scope": str(payload.get("scope") or "") if action_type == "refresh" else "",
                    "force": bool(payload.get("force")) if action_type == "refresh" else False,
                    "resumed_after_game": bool(payload.get("resumed_after_game")),
                }
            try:
                if action_type == "refresh":
                    result = self.core.refresh(
                        force=bool(payload.get("force")),
                        scope=str(payload.get("scope") or "due"),
                    )
                else:
                    result = self.core.set_private_stats(bool(payload.get("enabled")))
                final_status = (
                    "completed" if result.get("state") in {"ready", "degraded"} else "failed"
                )
                completed = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": final_status,
                    "started_at": started_at,
                    "completed_at": time.time(),
                    "result": result,
                }
            except Exception as exc:
                completed = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": "failed",
                    "started_at": started_at,
                    "completed_at": time.time(),
                    "result": {"state": "failed", "error_type": exc.__class__.__name__},
                }
            finally:
                with self._action_state_lock:
                    self._active_action = None
                    self._last_action = completed
                    self._completed_actions[action_id] = completed
                    while len(self._completed_actions) > 16:
                        self._completed_actions.pop(next(iter(self._completed_actions)))
                    self._queue_pending_refresh_locked()
                self._actions.task_done()

    def handler(self):
        from hextech.bootstrap.data_service_http import build_data_service_handler

        return build_data_service_handler(self, nonce_header=DATA_SERVICE_NONCE_HEADER)


def _terminal_refresh_state(result: Mapping[str, Any], reason: str) -> str:
    if result.get("refresh_state") == "deferred" or reason == "game_in_progress":
        return "deferred"
    if (
        result.get("state") == "failed"
        or ("failed" in reason and reason != "core_complete_optional_failed")
        or reason
        in {
            "data_stale",
            "optional_refresh_deferred",
            "refresh_backoff_pending",
            "shutdown_requested",
        }
    ):
        return "failed"
    if type(result.get("checked")) is bool:
        if not result.get("checked"):
            return "failed"
        if result.get("content_changed") or result.get("catalog_changed"):
            return "completed"
        return "unchanged"
    if reason in {"not_stale", "no_content_change"} or result.get(
        "promotion_disposition"
    ) == "unchanged":
        return "unchanged"
    return "completed"


def _refresh_status_payload(
    *,
    state: str,
    scope: str,
    phase: str,
    reason: str,
    generation_id: str,
    pending_sources: list[str] | None = None,
    started_at: float = 0.0,
    completed_at: float = 0.0,
    checked_at: float = 0.0,
    data_at: str = "",
    checked: bool = False,
    content_changed: bool = False,
    catalog_changed: bool = False,
    last_good_available: bool = False,
    source_outcomes: Mapping[str, Any] | None = None,
    catalog_state: str = "",
) -> dict[str, Any]:
    return {
        "state": state,
        "scope": scope,
        "phase": phase,
        "reason_code": reason,
        "generation_id": generation_id,
        "pending_sources": list(pending_sources or []),
        "started_at": started_at,
        "completed_at": completed_at,
        "checked_at": checked_at,
        "data_at": data_at,
        "checked": checked,
        "content_changed": content_changed,
        "catalog_changed": catalog_changed,
        "last_good_available": last_good_available,
        "source_outcomes": dict(source_outcomes or {}),
        "catalog_state": catalog_state,
    }


__all__ = ["DATA_SERVICE_NONCE_HEADER", "DataServiceApplication"]
