"""App 内唯一数据任务队列；不提供HTTP或独立进程入口。"""
from __future__ import annotations
import threading
import time
import os
from collections.abc import Mapping, Callable
from typing import Any
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.bootstrap.game_refresh_gate import normalize_refresh_scope
from hextech.bootstrap.data_service_status import sync_startup_snapshot_status as _sync_startup_snapshot_status
from hextech.bootstrap.data_service_application import DataServiceApplication
RefreshAction = Callable[[bool,str], Mapping[str,Any]]

class DataServiceCore:
    """DataService 唯一写入者的最小可测试核心。"""

    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        private_stats_enabled: bool,
        refresh_action: RefreshAction,
        initial_result: Mapping[str, Any] | None = None,
        progress_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.publisher = publisher
        self._private_stats_enabled = bool(private_stats_enabled)
        self._refresh_action = refresh_action
        self._progress_provider = progress_provider
        from hextech.modules.data.generation import DataSnapshotClient
        self._snapshot_client = DataSnapshotClient(publisher.root)
        self._status_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._last_result: dict[str, Any] = dict(initial_result or {"state": "starting", "generation_id": ""})
        _sync_startup_snapshot_status(self.publisher, self._last_result)

    def refresh(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        with self._action_lock:
            self._last_result = self._refresh_locked(force=force, scope=scope)
            _sync_startup_snapshot_status(self.publisher, self._last_result)
            return dict(self._last_result)

    def set_private_stats(self, enabled: bool) -> dict[str, Any]:
        with self._action_lock:
            self._private_stats_enabled = bool(enabled)
            self._last_result = {
                **self._last_result,
                "state": "ready" if self.publisher.current_generation_id() else self._last_result.get("state", "failed"),
                "generation_id": self.publisher.current_generation_id(),
                "desired_private_stats_enabled": self._private_stats_enabled,
                "reason_code": "display_policy_updated",
            }
            _sync_startup_snapshot_status(self.publisher, self._last_result)
            return dict(self._last_result)

    def status(self) -> dict[str, Any]:
        result = dict(self._last_result)
        if self._progress_provider is not None:
            result["executor_progress"] = dict(self._progress_provider())
        result["desired_private_stats_enabled"] = self._private_stats_enabled
        try:
            with self._status_lock:
                snapshot = self._snapshot_client.status()
        except Exception as exc:
            snapshot = {"state": "unavailable", "reason": str(exc)}
        result["snapshot"] = snapshot
        if snapshot.get("generation_id"):
            result["generation_id"] = snapshot["generation_id"]
        return result

    def _refresh_locked(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        normalized_scope = normalize_refresh_scope(scope)
        try:
            result = dict(self._refresh_action(bool(force), normalized_scope))
            result.setdefault("refresh_scope", normalized_scope)
            result.setdefault("force", bool(force))
            if result.get("state") == "degraded" and self.publisher.current_generation_id():
                optional_only = result.get("reason_code") in {"optional_source_stale", "core_complete_optional_failed"}
                result.setdefault(
                    "data_status",
                    "fresh" if optional_only else "data_stale",
                )
                result.setdefault(
                    "data_reason",
                    "optional_source_stale"
                    if optional_only
                    else "candidate_rejected_last_good_preserved",
                )
                outcomes = result.get("source_outcomes")
                source_last_good = isinstance(outcomes, Mapping) and any(
                    isinstance(outcome, Mapping) and outcome.get("used_last_good") is True
                    for outcome in outcomes.values()
                )
                try:
                    snapshot_last_good = bool(self._snapshot_client.status().get("generation_id"))
                except Exception:
                    snapshot_last_good = False
                result.setdefault(
                    "last_good_available",
                    source_last_good or snapshot_last_good,
                )
            return result
        except Exception as exc:
            try:
                fallback_status = self._snapshot_client.status()
                current_id = str(fallback_status.get("generation_id") or "")
            except Exception:
                # A pointer alone is not proof of a usable last-good snapshot.
                fallback_status = {}
                current_id = ""
            return {
                "state": "degraded" if current_id else "failed",
                "generation_id": current_id,
                "source": "last_good_fallback" if current_id else "remote_refresh",
                "reason_code": "refresh_failed_last_good_preserved" if current_id else "refresh_failed_no_snapshot",
                "data_status": "data_stale" if current_id else "unavailable",
                "data_reason": "refresh_exception_last_good_preserved" if current_id else "no_snapshot",
                "error_type": exc.__class__.__name__,
                "refresh_scope": normalized_scope,
                "force": bool(force),
                "checked": False,
                "content_changed": False,
                "catalog_changed": False,
                "last_good_available": bool(current_id),
                "source_outcomes": {
                    str(source): {
                        "state": "last_good",
                        "checked": False,
                        "changed": False,
                        "availability": "available",
                        "used_last_good": True,
                        "data_status": str(detail.get("data_status") or "unknown"),
                        "reason_code": "refresh_exception_last_good_preserved",
                    }
                    for source, detail in (fallback_status.get("source_status") or {}).items()
                    if isinstance(source, str)
                    and isinstance(detail, Mapping)
                    and int(detail.get("record_count") or 0) > 0
                    and str(detail.get("data_status") or "") not in {"pending", "unavailable"}
                },
            }

class EmbeddedDataRuntime:
    def __init__(self, application, coordinator, *, initial_result=None):
        self.application = application
        self.coordinator = coordinator
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._schedule, name="app-data-schedule",daemon=True)
        self._thread.start()

    def poll(self):
        return 0 if self._closed else None

    def _schedule(self):
        skip = os.getenv("HEXTECH_APP_SKIP_AUTO_REFRESH", "").casefold() in {"1","true","yes"}
        if not skip:
            self._stop.wait(3.0)
            if not self._stop.is_set():
                self.application.submit_action("refresh", {"scope":"core"})
        deadline = time.monotonic()+self.coordinator.seconds_until_due()
        while not self._stop.wait(.5):
            if not skip and (self.coordinator.poll_context() or time.monotonic() >= deadline):
                self.application.submit_action("refresh")
                deadline=time.monotonic()+self.coordinator.seconds_until_due()

    def get_status(self):
        return self.application.status()

    def refresh(self, *, scope="due", force=False):
        return self.application.submit_action("refresh", {"scope":scope,"force":force})

    def set_private_stats(self, enabled, *, timeout=None):
        action = self.application.submit_action("set_private_stats", {"enabled":bool(enabled)})
        action_id=action.get("action_id")
        if not action_id:
            raise RuntimeError(str(action.get("reason_code") or "data_action_rejected"))
        deadline=time.monotonic()+timeout if timeout is not None else None
        while not self._stop.is_set():
            result=self.application.get_action(action_id)
            if result and result.get("status") in {"completed","failed"}:
                return dict(result.get("result") or {"state":"failed"})
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("data_action_timeout")
            self._stop.wait(.05)
        raise RuntimeError("data_runtime_stopped")

    def stop(self, timeout=5.0):
        self._stop.set()
        self.application.request_shutdown()
        self.coordinator.request_stop()
        self._thread.join(timeout=min(1.0,timeout))
        if not self.application.close(timeout=max(0.0,timeout-1.0)):
            raise TimeoutError("data_tasks_shutdown_incomplete")
        self._closed = True


def create_embedded_data_service():
    from hextech.infrastructure.persistence.release_seed import initialize_snapshot
    from hextech.infrastructure.sources.refresh_service import IncrementalRefreshService
    from hextech.modules.data.ports.paths import get_var_dir
    from hextech.modules.session.settings import load_ui_feature_flags
    from hextech.infrastructure.sources.download_context import read_priority_champion
    from hextech.infrastructure.sources.aramkit.service import probe_aramkit_upstream_marker
    from hextech.bootstrap.game_refresh_gate import probe_production_game_in_progress
    publisher=DataSnapshotPublisher()
    initial=initialize_snapshot(publisher)
    if initial.get("reason_code") == "migration_required":
        raise RuntimeError("migration_required: select an explicitly migrated v4 runtime")
    coordinator=IncrementalRefreshService(publisher=publisher,root=get_var_dir(),game_state_probe=probe_production_game_in_progress,
        champion_probe=read_priority_champion,marker_probe=lambda:probe_aramkit_upstream_marker(
            conditional_cache_root=get_var_dir()/"state"/"http-validators"))
    core=DataServiceCore(publisher=publisher,private_stats_enabled=bool(load_ui_feature_flags().get("private_policy_stats_enabled")),
        refresh_action=lambda force,scope:coordinator.refresh(force=force,scope=scope),initial_result=initial,
        progress_provider=coordinator.progress)
    return EmbeddedDataRuntime(DataServiceApplication(core=core), coordinator, initial_result=initial)
