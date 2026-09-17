"""Stats/Vision 身份观察、selection 边界切换与失败回滚。"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import psutil

from hextech.interfaces.overlay.lifecycle import stop_process
from hextech.interfaces.overlay.sidecar_liveness import read_sidecar_liveness

OVERLAY_HOST_VISIBILITY_STALE_SECONDS = 6.0


class VisionHandoffMixin:
    def _read_visibility_health(self) -> dict[str, str]:
        try:
            payload = json.loads(self._visibility_status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"build_id": "", "visible_reason": "", "functional_status": "unknown", "functional_reason": ""}
        if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) not in {1, 2}:
            return {"build_id": "", "visible_reason": "", "functional_status": "unknown", "functional_reason": "unknown_schema"}
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0.0 or time.time() - updated_at > OVERLAY_HOST_VISIBILITY_STALE_SECONDS:
            return {
                "build_id": str(payload.get("build_id") or ""),
                "visible_reason": "",
                "functional_status": "failed" if self._process_running(self.host_process) else "unknown",
                "functional_reason": "host_heartbeat_stale",
            }
        raw_decision = payload.get("decision")
        decision: Mapping[str, Any] = raw_decision if isinstance(raw_decision, Mapping) else {}
        schema_version = int(payload.get("schema_version") or 1)
        return {
            "build_id": str(payload.get("build_id") or ""),
            "visible_reason": str(decision.get("reason") or "").strip(),
            "functional_status": (
                str(payload.get("functional_status") or "unknown").strip()
                if schema_version >= 2
                else "ready"
            ),
            "functional_reason": str(payload.get("functional_reason") or "").strip(),
        }

    def _init_vision_handoff_state(self) -> None:
        self.active_vision_pool_fingerprint = ""
        self.active_recognition_catalog_id = ""
        self.pending_recognition_catalog_id = ""
        self._observed_catalog_pointer = ""
        self.active_vision_origin_generation_id = ""
        self.observed_data_generation_id = ""
        self.pending_vision_pool_fingerprint = ""
        self.pending_vision_origin_generation_id = ""
        self.vision_handoff_state = "idle"
        self._pending_vision_hint_cache: dict[str, Any] | None = None
        self._active_vision_hint_cache: dict[str, Any] | None = None
        self._prepared_vision_hint_cache: dict[str, Any] | None = None
        self._rollback_vision_hint_cache: dict[str, Any] | None = None
        self._rollback_vision_pool_fingerprint = ""
        self._rollback_vision_origin_generation_id = ""
        self._rollback_sidecar_process: Any | None = None
        self._rollback_cache_stats: dict[str, Any] = {}
        self._vision_recovery_identity: dict[str, str] | None = None
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

    def _prepare_active_vision_recovery_locked(self) -> bool:
        identity = {
            "vision_pool_fingerprint": str(self.active_vision_pool_fingerprint or ""),
            "vision_pool_origin_generation_id": str(self.active_vision_origin_generation_id or ""),
            "recognition_catalog_id": str(self.active_recognition_catalog_id or ""),
        }
        if not all(identity.values()):
            self.last_start_failure_kind = "sidecar_recovery_identity_missing"
            self._mark(
                status="error",
                phase="sidecar_recovery_blocked",
                error="Vision sidecar 旧识别身份不完整，拒绝在游戏中采用待切换 Catalog",
            )
            return False
        active_stats: dict[str, Any] = {}
        for candidate in (self._rollback_cache_stats, self.cache_stats):
            if (
                candidate.get("vision_pool_fingerprint") == identity["vision_pool_fingerprint"]
                and candidate.get("recognition_catalog_id") == identity["recognition_catalog_id"]
            ):
                active_stats = dict(candidate)
                break
        active_stats.update(identity)
        self.cache_stats = active_stats
        self.cache_status = "ready"
        self._prepared_vision_hint_cache = (
            dict(self._active_vision_hint_cache)
            if self._active_vision_hint_cache is not None else None
        )
        self._vision_recovery_identity = identity
        return True

    def _record_active_vision_identity_locked(self, recovery_identity: Mapping[str, Any]) -> None:
        identity = recovery_identity or self.cache_stats
        self.active_vision_pool_fingerprint = str(
            getattr(self.sidecar_process, "_hextech_vision_pool_fingerprint", "")
            or identity.get("vision_pool_fingerprint") or ""
        )
        self.active_recognition_catalog_id = str(
            getattr(self.sidecar_process, "_hextech_recognition_catalog_id", "")
            or identity.get("recognition_catalog_id") or ""
        )
        self.active_vision_origin_generation_id = str(
            getattr(self.sidecar_process, "_hextech_vision_origin_generation_id", "")
            or identity.get("vision_pool_origin_generation_id")
            or identity.get("vision_pool_generation_id") or ""
        )
        if not recovery_identity:
            self._active_vision_hint_cache = self._prepared_vision_hint_cache

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

            try:
                pointer = json.loads(
                    (default_snapshot_root() / "current.v2.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pointer = {}
            pointer_generation_id = str(
                pointer.get("current_generation_id") or ""
            ) if isinstance(pointer, Mapping) else ""
            from hextech.modules.data.catalog.versioned import catalog_current_path

            try:
                catalog_pointer = catalog_current_path().read_text(encoding="utf-8")
            except OSError:
                catalog_pointer = ""
            with self._lock:
                if (
                    (pointer_generation_id or catalog_pointer)
                    and pointer_generation_id == self.observed_data_generation_id
                    and self._last_generation_observation[1]
                    and catalog_pointer == self._observed_catalog_pointer
                ):
                    if (
                        self.vision_handoff_state in {"deferred_selection_active", "deferred_game_active"}
                        and not self._vision_switch_blocked()
                    ):
                        self.vision_handoff_state = "ready"
                    return {
                        "changed": False,
                        "state": self.vision_handoff_state,
                        "observed_data_generation_id": pointer_generation_id,
                        "vision_pool_fingerprint": self._last_generation_observation[1],
                        "recognition_catalog_id": self.pending_recognition_catalog_id or self.active_recognition_catalog_id,
                    }
            hint_cache = dict(self._prepare_data_func() or {})
            fingerprint = self._vision_pool_fingerprint_func(hint_cache)
        except Exception as exc:
            return {"changed": False, "state": "probe_failed", "error_type": exc.__class__.__name__}
        snapshot = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
        generation_id = str(snapshot.get("generation_id") or "")
        vision = hint_cache.get("vision") if isinstance(hint_cache.get("vision"), Mapping) else {}
        recognition_catalog_id = str(vision.get("catalog_generation_id") or "")
        with self._lock:
            self._observed_catalog_pointer = catalog_pointer
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
                if not self._vision_handoff_in_progress:
                    self.pending_vision_pool_fingerprint = ""
                    self.pending_vision_origin_generation_id = ""
                    self.pending_recognition_catalog_id = ""
                    self._pending_vision_hint_cache = None
            else:
                self.pending_vision_pool_fingerprint = fingerprint
                self.pending_vision_origin_generation_id = generation_id
                self.pending_recognition_catalog_id = recognition_catalog_id
                self._pending_vision_hint_cache = hint_cache
                self.vision_handoff_state = (
                    "deferred_selection_active" if self._selection_window_active() else
                    "deferred_game_active" if self._vision_switch_blocked() else "ready"
                )
            return {
                "changed": previous != (generation_id, fingerprint),
                "state": self.vision_handoff_state,
                "observed_data_generation_id": generation_id,
                "vision_pool_fingerprint": fingerprint,
                "recognition_catalog_id": recognition_catalog_id,
            }

    def _vision_switch_blocked(self) -> bool:
        if self._selection_window_active():
            return True
        probe = getattr(self, "_game_active_probe", None)
        try:
            return bool(probe()) if probe is not None else False
        except Exception:
            return True

    def prepare_vision_handoff(self) -> bool:
        with self._lock:
            if (
                not self.desired_enabled
                or not self.pending_vision_pool_fingerprint
                or self._vision_switch_blocked()
            ):
                return False
            self.vision_handoff_state = "prewarming"
            self._rollback_vision_pool_fingerprint = self.active_vision_pool_fingerprint
            self._rollback_vision_origin_generation_id = self.active_vision_origin_generation_id
            self._rollback_sidecar_process = self.sidecar_process
            self._rollback_cache_stats = dict(self.cache_stats)
            try:
                if self._active_vision_hint_cache is not None:
                    self._rollback_vision_hint_cache = dict(self._active_vision_hint_cache)
                else:
                    from hextech.modules.data.overlay_source import SharedOverlayDataSource

                    self._rollback_vision_hint_cache = SharedOverlayDataSource(
                        generation_id=self.active_vision_origin_generation_id,
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
        if not self._rollback_vision_hint_cache:
            return False
        if (self.sidecar_process is self._rollback_sidecar_process
                and self._process_running(self.sidecar_process) and self._sidecar_is_reusable()):
            self.cache_stats = dict(self._rollback_cache_stats)
            self.cache_status = "ready"
            self._prepared_vision_hint_cache = dict(self._rollback_vision_hint_cache)
            self._active_vision_hint_cache = dict(self._rollback_vision_hint_cache)
            self._observed_catalog_pointer = ""
            self.vision_handoff_state = "rolled_back"
            self._vision_handoff_in_progress = False
            self._mark(status="running", phase="vision_handoff_rolled_back", error=reason)
            return True
        if not stop_process(self.sidecar_process):
            return False
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
                self._vision_handoff_in_progress = False
                self._startup_hard_deadline = time.perf_counter() + 30.0
                self._prepared_vision_hint_cache = dict(self._rollback_vision_hint_cache)
            self.sidecar_process = self._start_sidecar_with_retry(generation, cancel_event)
            self._sidecar_started_at = self._now_func()
            if not self._process_running(self.sidecar_process):
                return False
            with self._lock:
                self.active_vision_pool_fingerprint = self._rollback_vision_pool_fingerprint
                self.active_vision_origin_generation_id = self._rollback_vision_origin_generation_id
                self._active_vision_hint_cache = dict(self._rollback_vision_hint_cache)
                self._observed_catalog_pointer = ""
                self.active_recognition_catalog_id = str(stats.get("recognition_catalog_id") or "")
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
