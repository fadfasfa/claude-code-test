"""Stats/Vision 身份观察、selection 边界切换与失败回滚。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import psutil

from hextech.interfaces.overlay.lifecycle import stop_process
from hextech.interfaces.overlay.sidecar_liveness import read_sidecar_liveness


class VisionHandoffMixin:
    def _init_vision_handoff_state(self) -> None:
        self.active_vision_pool_fingerprint = ""
        self.active_vision_origin_generation_id = ""
        self.observed_data_generation_id = ""
        self.pending_vision_pool_fingerprint = ""
        self.pending_vision_origin_generation_id = ""
        self.vision_handoff_state = "idle"
        self._pending_vision_hint_cache: dict[str, Any] | None = None
        self._rollback_vision_hint_cache: dict[str, Any] | None = None
        self._rollback_vision_pool_fingerprint = ""
        self._rollback_vision_origin_generation_id = ""
        self._vision_handoff_in_progress = False
        self._last_generation_observation: tuple[str, str] = ("", "")

    @staticmethod
    def _process_running(process: Any | None) -> bool:
        return bool(process is not None and process.poll() is None)

    @staticmethod
    def _default_process_create_time(pid: int) -> float | None:
        try:
            return float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError):
            return None

    def _host_pid(self) -> int | None:
        return getattr(self.host_process, "_hextech_overlay_runtime_pid", None) or getattr(
            self.host_process,
            "pid",
            None,
        )

    def _sidecar_pid(self) -> int | None:
        return getattr(self.sidecar_process, "pid", None)

    def _read_sidecar_liveness(self) -> dict[str, Any]:
        return read_sidecar_liveness(
            self._sidecar_status_file,
            pid=self._sidecar_pid(),
            process_running=self._process_running(self.sidecar_process),
            sidecar_started_at=self._sidecar_started_at,
            now=self._now_func(),
            pid_exists=self._pid_exists,
            process_create_time=self._process_create_time,
        )

    def _sidecar_is_reusable(self) -> bool:
        liveness = self._read_sidecar_liveness()
        self._sidecar_liveness = liveness
        return liveness.get("status") in {"running", "starting"}

    def _mark_sidecar_stale_locked(self) -> None:
        liveness = self._read_sidecar_liveness()
        self._sidecar_liveness = liveness
        if liveness.get("status") not in {"running", "starting"}:
            self._mark(
                status="stale",
                phase="sidecar_stale",
                error=f"Vision sidecar 存活失效：{liveness.get('reason') or 'unknown'}",
            )

    def prepare_sidecar_restart(self) -> bool:
        with self._lock:
            if not self.desired_enabled or self.status != "stale":
                return False
            if self._sidecar_is_reusable():
                self._mark(status="running", phase="sidecar_recovered", error="")
                return False
            self._mark(status="starting", phase="sidecar_restart", error="")
            return True

    def _selection_window_active(self) -> bool:
        try:
            payload = json.loads(self._visibility_status_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        scene = payload.get("scene") if isinstance(payload, Mapping) else None
        return bool(isinstance(scene, Mapping) and scene.get("selection_window_active"))

    def observe_data_generation(self) -> dict[str, Any]:
        try:
            from hextech.modules.data.generation import default_snapshot_root

            pointer = json.loads(
                (default_snapshot_root() / "current.v2.json").read_text(encoding="utf-8")
            )
            pointer_generation_id = str(
                pointer.get("current_generation_id") or ""
            ) if isinstance(pointer, Mapping) else ""
            with self._lock:
                if (
                    pointer_generation_id
                    and pointer_generation_id == self.observed_data_generation_id
                    and self._last_generation_observation[1]
                ):
                    if (
                        self.vision_handoff_state == "deferred_selection_active"
                        and not self._selection_window_active()
                    ):
                        self.vision_handoff_state = "ready"
                    return {
                        "changed": False,
                        "state": self.vision_handoff_state,
                        "observed_data_generation_id": pointer_generation_id,
                        "vision_pool_fingerprint": self._last_generation_observation[1],
                    }
            hint_cache = dict(self._prepare_data_func() or {})
            fingerprint = self._vision_pool_fingerprint_func(hint_cache)
        except Exception as exc:
            return {"changed": False, "state": "probe_failed", "error_type": exc.__class__.__name__}
        snapshot = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
        generation_id = str(snapshot.get("generation_id") or "")
        with self._lock:
            previous = self._last_generation_observation
            self._last_generation_observation = (generation_id, fingerprint)
            self.observed_data_generation_id = generation_id
            active_fingerprint = (
                self.active_vision_pool_fingerprint
                or str(self.cache_stats.get("vision_pool_fingerprint") or "")
                or str(self._sidecar_liveness.get("vision_pool_fingerprint") or "")
            )
            if not active_fingerprint or fingerprint == active_fingerprint:
                self.vision_handoff_state = "stats_only" if generation_id else "idle"
            else:
                self.pending_vision_pool_fingerprint = fingerprint
                self.pending_vision_origin_generation_id = generation_id
                self._pending_vision_hint_cache = hint_cache
                self.vision_handoff_state = (
                    "deferred_selection_active" if self._selection_window_active() else "ready"
                )
            return {
                "changed": previous != (generation_id, fingerprint),
                "state": self.vision_handoff_state,
                "observed_data_generation_id": generation_id,
                "vision_pool_fingerprint": fingerprint,
            }

    def prepare_vision_handoff(self) -> bool:
        with self._lock:
            if (
                not self.desired_enabled
                or not self.pending_vision_pool_fingerprint
                or self._selection_window_active()
            ):
                return False
            self.vision_handoff_state = "prewarming"
            self._rollback_vision_pool_fingerprint = self.active_vision_pool_fingerprint
            self._rollback_vision_origin_generation_id = self.active_vision_origin_generation_id
            try:
                from hextech.modules.data.overlay_source import SharedOverlayDataSource

                self._rollback_vision_hint_cache = SharedOverlayDataSource(
                    generation_id=self.active_vision_origin_generation_id
                ).read_hint_cache()
            except Exception:
                self._rollback_vision_hint_cache = None
            self._vision_handoff_in_progress = True
            self.cache_status = "queued"
            self._prewarm_thread = None
        self.start_template_prewarm()
        return True

    def _rollback_vision_handoff(
        self,
        reason: str,
        generation: int,
        cancel_event: Any,
    ) -> bool:
        if not self._rollback_vision_hint_cache or not self._rollback_vision_origin_generation_id:
            return False
        stop_process(self.sidecar_process)
        self.sidecar_process = None
        try:
            runtime = self._template_loader()(
                hint_cache=dict(self._rollback_vision_hint_cache),
                require_production_pool=True,
            )
            stats = getattr(runtime, "stats", None)
            if not isinstance(stats, Mapping):
                return False
            with self._lock:
                self.cache_stats = dict(stats)
                self.cache_status = "ready"
            self.sidecar_process = self._start_sidecar_with_retry(generation, cancel_event)
            self._sidecar_started_at = self._now_func()
            if not self._process_running(self.sidecar_process):
                return False
            with self._lock:
                self.active_vision_pool_fingerprint = self._rollback_vision_pool_fingerprint
                self.active_vision_origin_generation_id = self._rollback_vision_origin_generation_id
                self.vision_handoff_state = "rolled_back"
                self._vision_handoff_in_progress = False
                self.last_start_failure_kind = "vision_handoff_failed_rolled_back"
                self._mark(
                    status="running",
                    phase="vision_handoff_rolled_back",
                    error=f"Vision handoff 失败，已恢复旧运行态：{reason}",
                )
            return True
        except Exception:
            return False


__all__ = ["VisionHandoffMixin"]
