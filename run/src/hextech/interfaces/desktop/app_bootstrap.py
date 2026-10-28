"""Desktop DesktopBootstrapMixin 职责模块。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from hextech.interfaces.desktop.app_shared import UI_COLORS, _empty_champions, logger, os, ui_runtime

if TYPE_CHECKING:
    from .service_manager import ServiceManager


class DesktopBootstrapMixin:
    def _register_pending_process_job(self, pid: int, job_object: object | None) -> None:
        """登记尚未完成 bootstrap 的 Job；退出路径可立即关闭它。"""

        pid_value = int(pid or 0)
        if pid_value <= 0:
            return
        close_immediately = False
        with self._pending_process_jobs_lock:
            if job_object is None:
                self._pending_process_jobs.pop(pid_value, None)
            elif self._closing:
                close_immediately = True
            else:
                self._pending_process_jobs[pid_value] = job_object
        if close_immediately:
            close = getattr(job_object, "close", None)
            if callable(close):
                close()

    def _close_pending_process_jobs(self) -> None:
        with self._pending_process_jobs_lock:
            pending = list(self._pending_process_jobs.values())
            self._pending_process_jobs.clear()
        for job_object in pending:
            close = getattr(job_object, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("关闭 bootstrap pending Job 失败。", exc_info=True)

    def _mark_first_idle_visible(self) -> None:
        # 隐藏启动也必须引导服务；窗口实际映射由呈现 owner 单独记录。
        self.startup_timing.mark("ui_ready", window_visible=False)
        self._set_status("控制界面已就绪，后台服务启动中...", UI_COLORS["warn"])

    def _schedule_post_visible_bootstrap(self) -> None:
        if self._post_visible_bootstrap_started or self._closing:
            return
        self._post_visible_bootstrap_started = True
        self._set_status("后台初始化中...", UI_COLORS["warn"])
        self._start_tracked_thread(self._post_visible_bootstrap, name="hextech-post-visible-bootstrap")

    def _post_visible_bootstrap(self) -> None:
        """首屏可见后再启动重型服务，避免 Tk shell 被后台依赖阻塞。"""

        error: Exception | None = None
        loaded_champions = _empty_champions()
        service_manager = None
        try:
            self.startup_timing.mark("background_bootstrap_start")
            if self._closing:
                return
            self._assert_no_foreign_runtime_roles()
            self.startup_timing.mark("release_seed_start", owner="app")
            seed_result = self._seed_initializer()
            if isinstance(seed_result, dict) and seed_result.get("state") not in {"ready", "degraded"}:
                reason = str(seed_result.get("reason_code") or seed_result.get("reason") or "release_seed_validation_failed")
                raise RuntimeError(reason)
            self.startup_timing.mark("release_seed_done", owner="app", result=str(seed_result or ""))
            from hextech.modules.data.generation import DataSnapshotClient
            from hextech.modules.data.catalog.version_catalog import load_champion_core_data
            from .service_manager import ServiceManager

            if self._closing:
                return
            if self._data_runtime_factory is None:
                raise RuntimeError("data runtime factory was not composed by App bootstrap")
            service_manager = ServiceManager(start_data_service_func=self._data_runtime_factory,
                listener_snapshot_func=self._desktop_window_presentation.status)
            if self._windows_session_end_pending:
                service_manager.set_shutdown_requested(True)
            if self._closing:
                return
            def start_data_service() -> None:
                try:
                    self.data_service = service_manager.start_data_service()
                except Exception:
                    self.data_service = None
                    logger.exception("DataService 启动失败，继续探测上一代本地快照。")

            # DataService 与 App owner 并行启动；控制面 ready 后再开始本地数据读取，
            # 保持“先可控制 Overlay、后加载列表”的既有故障隔离合同。
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="hextech-bootstrap") as executor:
                data_future = executor.submit(start_data_service)
                owner_future = executor.submit(
                    self._start_runtime_owner,
                    restore_persisted_game_overlay=False,
                )
                owner_future.result()
                if self._windows_session_end_pending:
                    service_manager.shutdown(timeout_seconds=0.5, final_timeout_seconds=4.5)
                    return
                self._run_on_ui_thread(self._activate_overlay_control_plane)

                def load_local_snapshot() -> list[dict]:
                    self.core_data = load_champion_core_data()
                    self._snapshot_client = DataSnapshotClient()
                    return self.load_data()

                snapshot_future = executor.submit(load_local_snapshot)
                data_future.result()
                loaded_champions = snapshot_future.result()
            if self._closing:
                self.runtime_owner.stop()
                self.runtime_owner = None
                service_manager.shutdown()
                self.data_service = None
                return
            service_manager.set_low_frequency_listener_enabled(
                self.feature_flags.get("low_frequency_listener_enabled", True)
            )
            if not self._publish_service_manager(service_manager):
                return
            self.start_background_runtime_monitor()
            self.startup_timing.mark("services_ready")
            self._run_on_ui_thread(self._apply_persisted_feature_flags)
            self.startup_timing.mark("data_ready", rows=len(loaded_champions))
        except Exception as exc:
            error = exc
            if self.service_manager is None:
                self._shutdown_failed_bootstrap_service_manager(service_manager)
            logger.exception("桌面后台初始化失败。")

        def finish() -> None:
            if self._closing:
                return
            if error is not None:
                self.startup_timing.mark("background_bootstrap_error", error=str(error))
                self._post_visible_bootstrap_done = True
                self._set_status(f"启动失败，数据与识别尚不可用: {error}", UI_COLORS["error"])
                from .startup_failure import show_startup_failure
                from hextech.modules.data.ports.paths import get_var_dir

                try:
                    diagnostic_path = str(get_var_dir() / "logs" / "hextech_error.log")
                except Exception:
                    diagnostic_path = ""
                show_startup_failure(error, diagnostic_path)
                return
            with self._champions_lock:
                self.champions = loaded_champions
            self._post_visible_bootstrap_done = True
            self.startup_timing.mark("background_bootstrap_done")
            self._set_status("后台服务已就绪", UI_COLORS["green"])
            self.update_ui(self.current_candidate_groups)
            self._init_core_engine()
            self.check_and_sync_data()
            self.start_background_scraper()

        self._run_on_ui_thread(finish)

    def _assert_no_foreign_runtime_roles(self) -> None:
        """只用受管状态身份阻止跨 Build/孤儿角色，不读取命令行也不杀进程。"""

        import json
        from pathlib import Path

        import psutil

        from hextech.modules.data.ports.paths import get_var_dir

        root = get_var_dir()
        conflicts: list[str] = []
        for role, relative in (
            ("host", "state/game_overlay_visibility.v1.json"),
            ("sidecar", "state/game_overlay_sidecar_status.json"),
        ):
            path = root / Path(relative)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                pid = int(payload.get("pid") or 0)
                build_id = str(payload.get("build_id") or "")
                expected_started = float(payload.get("pid_started_at") or 0.0)
                expected_executable = os.path.normcase(str(payload.get("executable") or ""))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            try:
                process = psutil.Process(pid)
                executable = os.path.normcase(process.exe())
                identity_matches = bool(
                    process.is_running()
                    and expected_started > 0.0
                    and abs(float(process.create_time()) - expected_started) <= 1.0
                    and expected_executable
                    and executable == expected_executable
                )
            except psutil.Error:
                identity_matches = False
                executable = "unknown"
            if not identity_matches:
                continue
            conflicts.append(f"{role}:pid={pid}:build={build_id or 'unknown'}:exe={executable}")
        if conflicts:
            raise RuntimeError("检测到未由当前 Desktop 持有的运行角色：" + "; ".join(conflicts))

    def _publish_service_manager(self, service_manager: "ServiceManager") -> bool:
        """发布后台 ServiceManager；若关闭已开始，则由 bootstrap 线程自清理。"""

        with self._service_manager_lock:
            if self._closing:
                should_shutdown = True
            else:
                self.service_manager = service_manager
                self._runtime_services_ready = True
                self._service_manager_shutdown_completed = None
                should_shutdown = False
        if should_shutdown:
            service_manager.shutdown()
            return False
        return True

    def _take_service_manager_for_shutdown(self) -> "ServiceManager | None":
        """关闭路径独占取走 ServiceManager，避免与 bootstrap 失败清理重复 shutdown。"""

        with self._service_manager_lock:
            service_manager = self.service_manager
            self.service_manager = None
            self._runtime_services_ready = False
            self._service_manager_shutdown_in_progress = service_manager
            return service_manager

    def _shutdown_failed_bootstrap_service_manager(self, service_manager: "ServiceManager | None") -> None:
        """bootstrap 失败时清理本轮创建的 ServiceManager，包含已发布和未发布两种状态。"""

        if service_manager is None:
            return
        with self._service_manager_lock:
            if self.service_manager is service_manager:
                self.service_manager = None
                self._runtime_services_ready = False
                should_shutdown = True
            elif self._service_manager_shutdown_in_progress is service_manager:
                should_shutdown = False
            elif self._service_manager_shutdown_completed is service_manager:
                should_shutdown = False
            else:
                should_shutdown = self.service_manager is not service_manager
        if should_shutdown:
            try:
                service_manager.shutdown()
            except Exception:
                logger.debug("后台初始化失败后清理 ServiceManager 失败。", exc_info=True)

    def _start_runtime_owner(self, *, restore_persisted_game_overlay=True, startup_timeout=None):
        if self._closing or self._windows_session_end_pending:
            return
        if self._runtime_owner_factory is None:
            raise RuntimeError("runtime owner factory was not composed by App bootstrap")
        owner = self._runtime_owner_factory()
        self.runtime_owner = owner
        if self._closing or self._windows_session_end_pending:
            owner.stop()
            self.runtime_owner = None
            return
        owner.start_overlay_template_prewarm()
        if restore_persisted_game_overlay:
            self._restore_persisted_game_overlay()

    def _init_core_engine(self):
        ui_runtime.initialize_window_threads(self)
