"""Desktop DesktopBootstrapMixin 职责模块。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from hextech.interfaces.desktop.app_shared import UI_COLORS, _empty_champions, logger, os, threading, ui_runtime

if TYPE_CHECKING:
    from .service_manager import ServiceManager


class DesktopBootstrapMixin:
    def _mark_first_idle_visible(self) -> None:
        self.startup_timing.mark("first_idle_visible")
        self._set_status("窗口已就绪，后台服务启动中...", UI_COLORS["warn"])

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
            from hextech.modules.data.generation import DataSnapshotClient
            from hextech.modules.data.catalog.version_catalog import load_champion_core_data
            from .service_manager import ServiceManager

            if self._closing:
                return
            service_manager = ServiceManager(
                start_web_func=self._spawn_web_process,
                start_data_service_func=lambda: ui_runtime.start_data_service_process(parent_pid=os.getpid()),
                stop_data_service_func=ui_runtime.stop_data_service_process,
                manage_overlay_runtime=False,
                listener_interval_seconds=3.0,
            )
            try:
                self.data_service = service_manager.start_data_service()
            except Exception:
                self.data_service = None
                logger.exception("DataService 启动失败，继续探测上一代本地快照。")
            self._start_runtime_supervisor(restore_persisted_game_overlay=False)
            self._run_on_ui_thread(self._activate_overlay_control_plane)
            if self._closing:
                ui_runtime.stop_runtime_supervisor_process(self.runtime_supervisor)
                self.runtime_supervisor = None
                service_manager.shutdown()
                self.data_service = None
                return
            service_manager.set_low_frequency_listener_enabled(
                self.feature_flags.get("low_frequency_listener_enabled", True)
            )
            service_manager.start_low_frequency_listener()
            if not self._publish_service_manager(service_manager):
                return
            self.startup_timing.mark("services_ready")
            self._run_on_ui_thread(self._apply_persisted_feature_flags)

            self.core_data = load_champion_core_data()
            self._snapshot_client = DataSnapshotClient()
            loaded_champions = self.load_data()
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
                self._set_status(f"本地数据初始化失败，展示面继续运行: {error}", UI_COLORS["warn"])
                return
            with self._champions_lock:
                self.champions = loaded_champions
            self._post_visible_bootstrap_done = True
            self.startup_timing.mark("background_bootstrap_done")
            self._set_status("后台服务已就绪", UI_COLORS["green"])
            self._init_core_engine()
            self.check_and_sync_data()
            self.start_background_scraper()

        self._run_on_ui_thread(finish)

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

    def _start_runtime_supervisor(self, *, restore_persisted_game_overlay: bool = True) -> None:
        """启动独立执行面，并用非 UI 线程续租，避免 Tk 主循环卡顿误杀运行态。"""

        try:
            self.runtime_supervisor = ui_runtime.start_runtime_supervisor_process(
                parent_pid=os.getpid(),
                prewarm_templates=True,
            )
            self._start_supervisor_lease_thread()
            if restore_persisted_game_overlay:
                self._restore_persisted_game_overlay()
        except Exception:
            logger.exception("Runtime Supervisor 启动失败，游戏内显示控制面暂不可用。")
            self.runtime_supervisor = None

    def _start_supervisor_lease_thread(self) -> None:
        if self.runtime_supervisor is None:
            return
        if self._supervisor_lease_thread is not None and self._supervisor_lease_thread.is_alive():
            return

        def lease_loop() -> None:
            while not self._supervisor_lease_stop.wait(2.0):
                handle = self.runtime_supervisor
                if handle is None:
                    return
                try:
                    handle.renew_lease(control_instance_id=self._control_instance_id)
                except Exception:
                    logger.warning("Runtime Supervisor lease 续租失败。", exc_info=True)

        self._supervisor_lease_thread = threading.Thread(
            target=lease_loop,
            name="hextech-supervisor-lease",
            daemon=True,
        )
        self._supervisor_lease_thread.start()

    def _spawn_web_process(self):
        return ui_runtime.start_web_server_process(
            self.web_port_file,
            auto_open_browser=self.feature_flags.get("auto_open_browser", True),
        )

    def _start_web_server(self):
        """后台启动网页服务，避免阻塞界面线程。"""

        try:
            if self.service_manager is None:
                raise RuntimeError("后台服务尚未就绪")
            self.service_manager.start_web()
            self.web_process = self.service_manager.web.process
            if self.feature_flags.get("auto_open_browser", True):
                ui_runtime.open_companion_browser(self.web_port_file)
        except Exception as exc:
            print(f"\n启动网页服务失败: {exc}")

    def _init_core_engine(self):
        ui_runtime.initialize_core_threads(self)

    def _run_terminal(self):
        ui_runtime.run_terminal_loop(self)
