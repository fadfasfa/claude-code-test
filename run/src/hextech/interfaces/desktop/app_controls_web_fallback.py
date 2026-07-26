"""Desktop Overlay 的临时 Web 备份编排 mixin。

职责：Overlay 启动失败或降级时拉起/回收临时 Web 前端，并维护其所有权状态机
（用户接管、清理请求、代际取消）。从 app_controls 拆出以守住单文件 800 行的
可维护性门禁；仍以 mixin 方式与 DesktopControlsMixin 同实例协作，不单独实例化。

调用方: app_controls.DesktopControlsMixin; 关键依赖: app_shared、ui_runtime。
"""
from hextech.interfaces.desktop.app_shared import (
    Mapping,
    UI_COLORS,
    logger,
    threading,
    ui_runtime,
)


class DesktopOverlayWebFallbackMixin:
    def _sync_web_process_handle(self) -> None:
        service_manager = self.service_manager
        self.web_process = service_manager.web.process if service_manager is not None and service_manager.is_web_running() else None

    def _ensure_overlay_fallback_state(self) -> None:
        """兼容轻量测试对象和旧反序列化实例，惰性补齐 fallback 运行态。"""

        if not hasattr(self, "_web_operation_lock"):
            self._web_operation_lock = threading.Lock()
        if not hasattr(self, "_fallback_web_lock"):
            self._fallback_web_lock = threading.Lock()
        if not hasattr(self, "_fallback_web_owned"):
            self._fallback_web_owned = False
        if not hasattr(self, "_fallback_web_starting"):
            self._fallback_web_starting = False
        if not hasattr(self, "_fallback_web_stopping"):
            self._fallback_web_stopping = False
        if not hasattr(self, "_fallback_web_cleanup_requested"):
            self._fallback_web_cleanup_requested = False
        if not hasattr(self, "_fallback_web_user_adopted"):
            self._fallback_web_user_adopted = False
        if not hasattr(self, "_fallback_web_error"):
            self._fallback_web_error = ""
        if not hasattr(self, "_fallback_web_generation"):
            self._fallback_web_generation = 0
        if not hasattr(self, "_fallback_web_failed_key"):
            self._fallback_web_failed_key = ""

    def _user_web_enabled(self) -> bool:
        variable = getattr(self, "web_frontend_var", None)
        if variable is not None:
            return bool(variable.get())
        return bool(getattr(self, "feature_flags", {}).get("web_frontend_enabled", False))

    def _adopt_fallback_web_for_user(self) -> None:
        """用户主动开启 Web 后转移所有权，后续 Overlay 恢复不得自动关闭。"""

        self._ensure_overlay_fallback_state()
        with self._fallback_web_lock:
            self._fallback_web_generation += 1
            self._fallback_web_owned = False
            self._fallback_web_cleanup_requested = False
            self._fallback_web_user_adopted = True

    def _start_overlay_web_fallback(self, fallback_key: str) -> None:
        self._ensure_overlay_fallback_state()
        with self._fallback_web_lock:
            if self._fallback_web_owned or self._fallback_web_starting or self._fallback_web_stopping:
                return
            if self._fallback_web_failed_key == fallback_key:
                return
            self._fallback_web_generation += 1
            generation = self._fallback_web_generation
            self._fallback_web_starting = True
            self._fallback_web_cleanup_requested = False
            self._fallback_web_user_adopted = False
            self._fallback_web_error = ""

        def worker() -> None:
            error: Exception | None = None
            browser_opened = True
            manager = None
            should_cleanup = False
            with self._web_operation_lock:
                with self._fallback_web_lock:
                    cancelled = bool(
                        generation != self._fallback_web_generation
                        or self._fallback_web_user_adopted
                        or self._closing
                    )
                    if cancelled:
                        self._fallback_web_starting = False
                if cancelled:
                    return
                try:
                    manager = self.service_manager
                    if manager is None:
                        raise RuntimeError("后台服务尚未就绪")
                    manager.start_web()
                except Exception as exc:
                    error = exc
                if error is None and not self._closing:
                    try:
                        browser_opened = ui_runtime.open_companion_browser(self.web_port_file)
                    except Exception:
                        browser_opened = False
                        logger.warning("Overlay Web 备份已启动，但受管浏览器打开失败。", exc_info=True)

                with self._fallback_web_lock:
                    self._fallback_web_starting = False
                    user_owned = self._fallback_web_user_adopted or generation != self._fallback_web_generation
                    should_cleanup = bool(
                        error is None
                        and not user_owned
                        and (self._fallback_web_cleanup_requested or self._closing)
                    )
                    self._fallback_web_owned = bool(error is None and not user_owned and not should_cleanup)
                    self._fallback_web_error = str(error or "")
                    self._fallback_web_failed_key = fallback_key if error is not None else ""
                    self._fallback_web_cleanup_requested = False

                if should_cleanup:
                    ui_runtime.close_companion_browser()
                    try:
                        if manager is not None:
                            manager.stop_web()
                    except Exception as exc:
                        error = exc
                        with self._fallback_web_lock:
                            self._fallback_web_owned = True
                            self._fallback_web_error = str(exc)

            self._sync_web_process_handle()

            def finish() -> None:
                if error is not None:
                    self._set_overlay_status_summary(
                        f"游戏内显示: Web 备份启动失败 ({error})",
                        UI_COLORS["error"],
                    )
                elif should_cleanup:
                    self._set_overlay_status_summary("游戏内显示: Overlay 已恢复", UI_COLORS["green"])
                elif not browser_opened:
                    self._set_overlay_status_summary("游戏内显示: Web 备份已接管，浏览器打开失败", UI_COLORS["warn"])
                else:
                    self._set_overlay_status_summary("游戏内显示: Web 备份已接管", UI_COLORS["warn"])

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-overlay-web-fallback-start")

    def _request_overlay_web_fallback_cleanup(self) -> None:
        self._ensure_overlay_fallback_state()
        with self._fallback_web_lock:
            if self._fallback_web_starting:
                self._fallback_web_cleanup_requested = True
                return
            if not self._fallback_web_owned or self._fallback_web_stopping:
                return
            generation = self._fallback_web_generation
            self._fallback_web_stopping = True

        def worker() -> None:
            error: Exception | None = None
            with self._web_operation_lock:
                with self._fallback_web_lock:
                    cancelled = bool(
                        generation != self._fallback_web_generation
                        or self._fallback_web_user_adopted
                    )
                    if cancelled:
                        self._fallback_web_stopping = False
                if cancelled:
                    return
                manager = self.service_manager
                ui_runtime.close_companion_browser()
                try:
                    if manager is not None:
                        manager.stop_web()
                except Exception as exc:
                    error = exc
            with self._fallback_web_lock:
                self._fallback_web_stopping = False
                self._fallback_web_owned = error is not None
                self._fallback_web_error = str(error or "")
            self._sync_web_process_handle()

            def finish() -> None:
                if error is None:
                    self._set_overlay_status_summary("游戏内显示: Overlay 已恢复", UI_COLORS["green"])
                else:
                    self._set_overlay_status_summary(
                        f"游戏内显示: Overlay 已恢复，Web 备份关闭失败 ({error})",
                        UI_COLORS["warn"],
                    )

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-overlay-web-fallback-stop")

    def _coordinate_overlay_web_fallback(self, overlay: Mapping[str, object]) -> bool:
        """根据 Supervisor 状态编排临时 Web；返回是否已输出 fallback 专用状态。"""

        self._ensure_overlay_fallback_state()
        overlay_enabled = bool(self.game_overlay_var.get())
        status = str(overlay.get("status") or "")
        fallback_key = str(overlay.get("generation") or "default")
        fallback_recommended = bool(overlay.get("fallback_recommended")) or (
            overlay_enabled and status == "error"
        )
        if self._user_web_enabled():
            self._adopt_fallback_web_for_user()
        if not overlay_enabled:
            self._request_overlay_web_fallback_cleanup()
            return False
        if status == "running":
            with self._fallback_web_lock:
                self._fallback_web_failed_key = ""
            if not self._user_web_enabled():
                self._request_overlay_web_fallback_cleanup()
                with self._fallback_web_lock:
                    cleanup_active = self._fallback_web_stopping or self._fallback_web_cleanup_requested
                if cleanup_active:
                    self._set_overlay_status_summary("游戏内显示: Overlay 已恢复，正在关闭 Web 备份", UI_COLORS["green"])
                    return True
            return False
        if fallback_recommended:
            if not self._user_web_enabled():
                self._start_overlay_web_fallback(fallback_key)
            with self._fallback_web_lock:
                owned = self._fallback_web_owned
                starting = self._fallback_web_starting
                fallback_error = self._fallback_web_error
            if status == "error":
                if owned or self._user_web_enabled():
                    self._set_overlay_status_summary("游戏内显示: Overlay 最终失败 / Web 备份保留", UI_COLORS["error"])
                elif starting:
                    self._set_overlay_status_summary("游戏内显示: Overlay 最终失败 / Web 备份启动中", UI_COLORS["error"])
                else:
                    self._set_overlay_status_summary(
                        f"游戏内显示: Overlay 与 Web 备份均启动失败 ({fallback_error or 'unknown'})",
                        UI_COLORS["error"],
                    )
            elif starting:
                self._set_overlay_status_summary("游戏内显示: Web 备份启动中 / Overlay 继续启动", UI_COLORS["warn"])
            elif fallback_error:
                self._set_overlay_status_summary(
                    f"游戏内显示: Web 备份启动失败，Overlay 继续启动 ({fallback_error})",
                    UI_COLORS["error"],
                )
            else:
                self._set_overlay_status_summary("游戏内显示: Web 备份已接管 / Overlay 继续启动", UI_COLORS["warn"])
            return True
        return False

