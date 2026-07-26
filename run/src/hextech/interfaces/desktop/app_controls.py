"""Desktop DesktopControlsMixin 职责模块。"""
from hextech.interfaces.desktop.app_controls_web_fallback import DesktopOverlayWebFallbackMixin
from hextech.interfaces.desktop.app_shared import (
    UI_COLORS,
    _format_game_overlay_host_reason,
    _format_supervisor_game_overlay_status,
    export_user_diagnostics,
    logger,
    save_ui_feature_flags,
    scaled,
    threading,
    tk,
    ui_font,
    ui_runtime,
)


class DesktopControlsMixin(DesktopOverlayWebFallbackMixin):
    def _build_ui(self):
        scale = self._ui_scale_value()
        self.title_frame = tk.Frame(self.root, bg=UI_COLORS["header"])
        self.title_frame.pack(fill=tk.X)

        self.title_bar = tk.Label(
            self.title_frame,
            text="备战席",
            bg=UI_COLORS["header"],
            fg=UI_COLORS["gold"],
            font=ui_font(scale, 16, bold=True),
            pady=scaled(8, scale),
        )
        self.title_bar.bind("<ButtonPress-1>", self.start_move)
        self.title_bar.bind("<B1-Motion>", self.do_move)
        # 双击标题栏在展开/折叠宽度间切换，便于单屏游戏窗口模式让出主屏视线
        self.title_bar.bind("<Double-Button-1>", self._toggle_collapse)

        self.exit_button = tk.Button(
            self.title_frame,
            text="×",
            command=self.hide_to_tray,
            bg=UI_COLORS["header"],
            fg=UI_COLORS["red"],
            activebackground=UI_COLORS["red"],
            activeforeground=UI_COLORS["base"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            width=2,
            padx=0,
            pady=1,
            font=ui_font(scale, 15, bold=True),
            cursor="hand2",
        )
        # 右上角“×”只隐藏到托盘；完全退出必须使用托盘菜单，避免误杀识别进程。
        self.exit_button.pack(side=tk.RIGHT, padx=(0, 6), pady=5)

        # 可见折叠按钮：与双击标题等价，「«」收窄 /「»」展开
        self.collapse_button = tk.Button(
            self.title_frame,
            text="«",
            command=self._toggle_collapse,
            bg=UI_COLORS["header"],
            fg=UI_COLORS["muted"],
            activebackground=UI_COLORS["surface"],
            activeforeground=UI_COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            width=2,
            padx=0,
            pady=1,
            font=ui_font(scale, 15, bold=True),
            cursor="hand2",
        )
        self.collapse_button.pack(side=tk.RIGHT, padx=(0, 2), pady=5)

        self.diagnostics_button = tk.Button(
            self.title_frame,
            text="诊断",
            command=self._start_user_diagnostics_export,
            bg=UI_COLORS["surface_alt"],
            fg=UI_COLORS["muted"],
            activebackground=UI_COLORS["surface"],
            activeforeground=UI_COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=scaled(8, scale),
            pady=scaled(3, scale),
            font=ui_font(scale, 11, bold=True),
            cursor="hand2",
        )
        self.diagnostics_button.pack(side=tk.RIGHT, padx=(0, 8), pady=6)
        # 标题文本最后 pack：Tk pack 空间不足时裁后来者，折叠态必须保住右侧
        # 按钮（尤其「»」展开入口），被裁的只能是标题文字。
        self.title_bar.pack(side=tk.LEFT, padx=(scaled(10, scale), 0))

        self.feature_frame = tk.Frame(self.root, bg=UI_COLORS["base"], padx=10, pady=8)
        self.feature_frame.pack(fill=tk.X)
        self._feature_toggle_widgets = []
        self.web_frontend_var = tk.BooleanVar(value=self.feature_flags["web_frontend_enabled"])
        self.game_overlay_var = tk.BooleanVar(value=self.feature_flags["game_overlay_enabled"])
        self.private_stats_var = tk.BooleanVar(value=self.feature_flags["private_policy_stats_enabled"])
        self.low_frequency_listener_var = tk.BooleanVar(value=self.feature_flags["low_frequency_listener_enabled"])

        self.web_frontend_check = self._build_feature_toggle(
            "Web 前端",
            self.web_frontend_var,
            self._toggle_web_frontend,
            accent=UI_COLORS["cyan"],
        )
        self.web_frontend_check.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 4))
        self.game_overlay_check = self._build_feature_toggle(
            "游戏内显示",
            self.game_overlay_var,
            self._toggle_game_overlay,
            accent=UI_COLORS["cyan"],
        )
        self.game_overlay_check.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self.private_stats_check = self._build_feature_toggle(
            "私用统计",
            self.private_stats_var,
            self._toggle_private_policy_stats,
            accent=UI_COLORS["green"],
        )
        self.private_stats_check.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(2, 0))

        self.feature_frame.grid_columnconfigure(0, weight=1)
        self.feature_frame.grid_columnconfigure(1, weight=1)

        self.list_shell = tk.Frame(self.root, bg=UI_COLORS["base"])
        self.list_shell.pack(fill=tk.BOTH, expand=True, padx=(scaled(10, scale), 0), pady=(6, 4))
        self.canvas = tk.Canvas(self.list_shell, bg=UI_COLORS["base"], highlightthickness=0)
        self.list_frame = tk.Frame(self.canvas, bg=UI_COLORS["base"])
        # 细滚动条：给长列表一个可见的位置指示，滚轮行为保持不变
        self.list_scrollbar = tk.Scrollbar(
            self.list_shell,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
            width=scaled(8, scale),
        )
        self.canvas.configure(yscrollcommand=self.list_scrollbar.set)
        self.list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._list_window_id = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        # 卡片宽度必须跟随 canvas 实际宽度，否则 fill=X 只能撑到内容自然宽度
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._list_window_id, width=e.width),
        )

        def _on_mousewheel(event):
            # 滚轮只在指针位于列表区域内时生效，不再全局拦截整个应用的滚轮
            node = self.root.winfo_containing(event.x_root, event.y_root)
            while node is not None:
                if node is self.list_shell:
                    self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                    break
                node = getattr(node, "master", None)

        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.status_label = tk.Label(
            self.root,
            text="系统初始化中...",
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=ui_font(scale, 11),
        )
        self.status_label.pack(side=tk.BOTTOM, pady=5)
        self.overlay_status_label = tk.Label(
            self.root,
            text="",
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=ui_font(scale, 11),
        )
        self.overlay_status_label.pack(side=tk.BOTTOM, pady=(0, 2))
        self._refresh_feature_toggle_styles()

    def _build_feature_toggle(
        self,
        text: str,
        variable: "tk.BooleanVar",
        command,
        *,
        accent: str,
    ) -> "tk.Frame":
        scale = self._ui_scale_value()
        frame = tk.Frame(self.feature_frame, bg=UI_COLORS["base"], cursor="hand2", padx=2, pady=2)
        dot = tk.Canvas(
            frame,
            width=scaled(15, scale),
            height=scaled(15, scale),
            bg=UI_COLORS["base"],
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        dot.pack(side=tk.LEFT, padx=(0, 5))
        label = tk.Label(
            frame,
            text=text,
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=ui_font(scale, 12, bold=True),
            cursor="hand2",
        )
        label.pack(side=tk.LEFT)

        toggle = {"frame": frame, "dot": dot, "label": label, "variable": variable, "accent": accent}
        self._feature_toggle_widgets.append(toggle)

        def _on_click(_event=None) -> None:
            if self._feature_toggle_is_busy(text):
                if text == "游戏内显示":
                    self._set_overlay_status_summary("游戏内显示: 正在切换中", UI_COLORS["warn"])
                else:
                    self._set_status(f"{text} 正在切换中...", UI_COLORS["warn"])
                return "break"
            if text in {"Web 前端", "游戏内显示", "私用统计"} and not self._runtime_services_ready:
                self._set_status("后台服务仍在启动中，请稍候...", UI_COLORS["warn"])
                return "break"
            variable.set(not bool(variable.get()))
            self._refresh_feature_toggle_styles()
            command()
            return "break"

        for widget in (frame, dot, label):
            widget.bind("<Button-1>", _on_click)
        return frame

    def _refresh_feature_toggle_styles(self) -> None:
        scale = self._ui_scale_value()

        def px(value: int) -> int:
            return scaled(value, scale)

        for toggle in getattr(self, "_feature_toggle_widgets", []):
            variable = toggle["variable"]
            dot = toggle["dot"]
            label = toggle["label"]
            accent = toggle["accent"]
            name = str(label.cget("text"))
            enabled = bool(variable.get())
            busy = self._feature_toggle_is_busy(name)
            try:
                dot.delete("all")
                if enabled:
                    dot.create_oval(px(4), px(4), px(11), px(11), fill=accent, outline=accent)
                    dot.create_oval(px(2), px(2), px(13), px(13), outline=accent)
                    label.config(fg=UI_COLORS["warn"] if busy else UI_COLORS["text"])
                else:
                    dot.create_oval(px(4), px(4), px(11), px(11), fill="", outline=UI_COLORS["dim"])
                    label.config(fg=UI_COLORS["warn"] if busy else UI_COLORS["dim"])
            except tk.TclError:
                logger.debug("刷新功能开关样式失败。", exc_info=True)

    def _feature_toggle_is_busy(self, name: str) -> bool:
        with self._feature_toggle_lock:
            return name in self._feature_toggle_busy

    def _set_feature_toggle_busy(self, name: str, busy: bool) -> None:
        with self._feature_toggle_lock:
            if busy:
                self._feature_toggle_busy.add(name)
            else:
                self._feature_toggle_busy.discard(name)
        self._refresh_feature_toggle_styles()

    def _start_tracked_thread(self, target, *, name: str) -> threading.Thread:
        """启动并登记后台线程，退出时可等待，避免 overlay worker 留下孤儿进程。"""

        thread: threading.Thread

        def tracked_target() -> None:
            try:
                target()
            finally:
                with self._threads_lock:
                    if thread in self.threads:
                        self.threads.remove(thread)

        thread = threading.Thread(target=tracked_target, name=name, daemon=True)
        with self._threads_lock:
            self.threads.append(thread)
        thread.start()
        return thread

    def _start_user_diagnostics_export(self) -> None:
        """后台导出用户可发送的轻量诊断包，避免 UI 线程被 zip 写入阻塞。"""

        if hasattr(self, "diagnostics_button"):
            self.diagnostics_button.config(state=tk.DISABLED)
        self._set_status("正在导出诊断包...", UI_COLORS["warn"])

        def worker() -> None:
            try:
                result = export_user_diagnostics()
            except Exception as exc:
                logger.exception("用户诊断导出失败。")
                error_text = str(exc)

                def fail() -> None:
                    if hasattr(self, "diagnostics_button"):
                        self.diagnostics_button.config(state=tk.NORMAL)
                    self._set_status(f"诊断导出失败: {error_text}", UI_COLORS["error"])

                self._run_on_ui_thread(fail)
                return

            def finish() -> None:
                if hasattr(self, "diagnostics_button"):
                    self.diagnostics_button.config(state=tk.NORMAL)
                zip_path = result.zip_path
                self._set_status(f"诊断已导出: {zip_path}", UI_COLORS["green"])

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-user-diagnostics-export")

    def _collect_feature_flags_from_controls(self) -> dict:
        return {
            "web_frontend_enabled": bool(self.web_frontend_var.get()),
            "game_overlay_enabled": bool(self.game_overlay_var.get()),
            "auto_open_browser": bool(self.feature_flags.get("auto_open_browser", True)),
            "private_policy_stats_enabled": bool(self.private_stats_var.get()),
            "low_frequency_listener_enabled": bool(self.low_frequency_listener_var.get()),
        }

    def _persist_feature_flags_from_controls(self) -> None:
        self.feature_flags = save_ui_feature_flags(self._collect_feature_flags_from_controls())
        if self.service_manager is not None:
            self.service_manager.set_low_frequency_listener_enabled(self.feature_flags["low_frequency_listener_enabled"])

    def _try_persist_feature_flags_from_controls(self) -> None:
        try:
            self._persist_feature_flags_from_controls()
        except Exception:
            logger.exception("持久化 UI 功能开关失败。")

    def _restore_feature_toggle_after_failure(self, key: str, variable) -> None:
        """启动失败只回滚控件显示，不把失败态当成新的用户偏好落盘。"""

        variable.set(bool(self.feature_flags.get(key)))

    def _raise_if_service_error(self, service_name: str) -> None:
        if self.service_manager is None:
            raise RuntimeError("后台服务尚未就绪")
        service = getattr(self.service_manager, service_name)
        if service.status == "error":
            raise RuntimeError(service.last_error or f"{service_name} 状态异常")

    def _apply_persisted_feature_flags(self) -> None:
        if self.feature_flags.get("web_frontend_enabled"):
            self._toggle_web_frontend()
        # 游戏内显示依赖 Runtime Supervisor；Supervisor 延后到首屏绘制后启动，
        # 所以持久化恢复必须等控制面就绪后单独执行。
        self._game_overlay_desired_enabled = bool(self.feature_flags.get("game_overlay_enabled"))

    def _restore_persisted_game_overlay(self) -> None:
        if self.runtime_supervisor is None:
            return
        if not bool(self.feature_flags.get("game_overlay_enabled")):
            return
        if not bool(self.game_overlay_var.get()):
            return
        if self._feature_toggle_is_busy("游戏内显示"):
            return
        self._set_overlay_status_summary("游戏内显示: 正在恢复", UI_COLORS["warn"])
        try:
            self._toggle_game_overlay()
        except Exception:
            logger.exception("恢复持久化游戏内显示失败。")

    def _activate_overlay_control_plane(self) -> None:
        """本地数据可降级，但 Overlay 恢复和预算轮询不能被它阻塞。"""

        if self._closing:
            return
        self._restore_persisted_game_overlay()
        if self._overlay_status_after_id is None:
            self._start_overlay_status_polling()

    def _toggle_web_frontend(self) -> None:
        toggle_name = "Web 前端"
        enabled = bool(self.web_frontend_var.get())
        if enabled:
            self._adopt_fallback_web_for_user()
        self._set_feature_toggle_busy(toggle_name, True)
        self._set_status("正在切换 Web 前端...", UI_COLORS["warn"])

        def worker() -> None:
            error: Exception | None = None
            browser_opened = True
            self._ensure_overlay_fallback_state()
            with self._web_operation_lock:
                try:
                    if self.service_manager is None:
                        raise RuntimeError("后台服务尚未就绪")
                    if enabled:
                        self.service_manager.start_web()
                        if self.feature_flags.get("auto_open_browser", True):
                            browser_opened = ui_runtime.open_companion_browser(self.web_port_file)
                    else:
                        ui_runtime.close_companion_browser()
                        self.service_manager.stop_web()
                        self._raise_if_service_error("web")
                except Exception as exc:
                    error = exc
                    if enabled and self.service_manager is not None:
                        ui_runtime.close_companion_browser()
                        self.service_manager.stop_web()

            def finish() -> None:
                self._sync_web_process_handle()
                if error is None:
                    self._persist_feature_flags_from_controls()
                    if enabled and not browser_opened:
                        self._set_status("Web 已启动，浏览器未自动打开", UI_COLORS["warn"])
                    else:
                        self._set_status("Web 前端已启动" if enabled else "Web 前端已关闭", UI_COLORS["green"] if enabled else UI_COLORS["muted"])
                else:
                    self._restore_feature_toggle_after_failure("web_frontend_enabled", self.web_frontend_var)
                    self._set_status(f"Web 前端切换失败: {error}", UI_COLORS["error"])
                self._set_feature_toggle_busy(toggle_name, False)

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-toggle-web")

    def _toggle_game_overlay(self) -> None:
        toggle_name = "游戏内显示"
        enabled = bool(self.game_overlay_var.get())
        if not enabled:
            self._request_overlay_web_fallback_cleanup()
        self._game_overlay_desired_enabled = enabled
        self._set_feature_toggle_busy(toggle_name, True)
        self._set_overlay_status_summary(
            "游戏内显示: 正在提交启动请求" if enabled else "游戏内显示: 正在提交关闭请求",
            UI_COLORS["warn"],
        )

        def worker() -> None:
            error: Exception | None = None
            action: dict | None = None
            try:
                with self._overlay_operation_lock:
                    if self._closing:
                        return
                    if self.runtime_supervisor is None:
                        raise RuntimeError("Runtime Supervisor 未启动")
                    action = self.runtime_supervisor.set_game_overlay_enabled(enabled)
            except Exception as exc:
                error = exc

            def finish() -> None:
                if error is None:
                    self._persist_feature_flags_from_controls()
                    action_status = str((action or {}).get("status") or "accepted")
                    status_color = UI_COLORS["green"] if action_status == "completed" and enabled else UI_COLORS["warn"]
                    self._set_overlay_status_summary(
                        f"游戏内显示启动请求已提交({action_status})" if enabled else f"游戏内显示关闭请求已提交({action_status})",
                        status_color if enabled else UI_COLORS["muted"],
                    )
                else:
                    self._restore_feature_toggle_after_failure("game_overlay_enabled", self.game_overlay_var)
                    self._game_overlay_desired_enabled = bool(self.feature_flags.get("game_overlay_enabled"))
                    self._set_overlay_status_summary(f"游戏内显示切换失败: {error}", UI_COLORS["error"])
                self._set_feature_toggle_busy(toggle_name, False)

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-toggle-overlay")

    def _toggle_private_policy_stats(self) -> None:
        toggle_name = "私用统计"
        desired_private_stats = bool(self.private_stats_var.get())
        self._set_feature_toggle_busy(toggle_name, True)
        self._set_status("正在更新私用统计缓存...", UI_COLORS["warn"])

        def worker() -> None:
            error: Exception | None = None
            try:
                if self.data_service is None:
                    raise RuntimeError("DataService 尚未就绪")
                result = self.data_service.set_private_stats(desired_private_stats, timeout=None)
                if result.get("state") not in {"ready", "degraded"}:
                    raise RuntimeError(str(result.get("reason_code") or "DataService 更新失败"))
            except Exception as exc:
                error = exc

            def finish() -> None:
                if error is None:
                    self._persist_feature_flags_from_controls()
                    self._set_status("私用统计仅用于本机实验，存在 Riot policy 风险", UI_COLORS["warn"])
                else:
                    self.private_stats_var.set(bool(self.feature_flags.get("private_policy_stats_enabled", True)))
                    self._set_status(f"私用统计切换失败: {error}", UI_COLORS["error"])
                self._set_feature_toggle_busy(toggle_name, False)

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-toggle-private-stats")

    def _toggle_low_frequency_listener(self) -> None:
        self._persist_feature_flags_from_controls()

    def check_and_sync_data(self):
        logger.info("桌面不直接刷新数据：refresh 与 generation 发布由 DataService 负责。")

    def _set_status(self, text, color):
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.config(text=text, fg=color)

    def _set_overlay_status_summary(self, text: str, color: str) -> None:
        """只更新游戏内显示的二级状态，不覆盖主服务/英雄状态栏。"""

        self._overlay_status_text = str(text or "")
        self._overlay_status_color = color
        if hasattr(self, "overlay_status_label") and self.overlay_status_label.winfo_exists():
            self.overlay_status_label.config(text=self._overlay_status_text, fg=color)

    def _start_overlay_status_polling(self) -> None:
        self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)

    def _kick_game_overlay_watchdog(self) -> None:
        if self.runtime_supervisor is not None:
            return
        if self._closing or self._feature_toggle_is_busy("游戏内显示"):
            return
        if not self._overlay_watchdog_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                with self._overlay_operation_lock:
                    if self._closing:
                        return
                    if self.service_manager is None:
                        return
                    self.service_manager.ensure_game_overlay_healthy(
                        enabled=self._game_overlay_desired_enabled,
                    )
            except Exception:
                logger.warning("游戏内 overlay watchdog 自愈失败。", exc_info=True)
            finally:
                self._overlay_watchdog_lock.release()

        self._start_tracked_thread(worker, name="hextech-overlay-watchdog")

    def _refresh_overlay_status_summary(self) -> None:
        """低频回显游戏内 overlay 状态，避免 running 但不可见时没有反馈。"""

        try:
            runtime_state = str(getattr(self, "_background_runtime_state", "running"))
            if runtime_state != "running":
                text = {
                    "suspending": "游戏内显示: 正在进入轻量待机",
                    "suspended": "游戏内显示: 识别已休眠",
                    "resuming": "游戏内显示: 识别恢复中",
                    "restart_in_progress": "游戏内显示: 识别重启中",
                    "resume_failed": "游戏内显示: 识别恢复失败",
                }.get(runtime_state, "游戏内显示: 后台状态未知")
                self._set_overlay_status_summary(text, UI_COLORS["warn"])
                return
            overlay_enabled = bool(self.game_overlay_var.get())
            if self.runtime_supervisor is not None:
                snapshot = self.runtime_supervisor.get_status()
                components = snapshot.get("components") if isinstance(snapshot.get("components"), dict) else {}
                overlay = components.get("game_overlay") if isinstance(components.get("game_overlay"), dict) else {}
                should_report = overlay_enabled or str(overlay.get("status") or "") in {"starting", "running", "stopping", "error"}
                if should_report:
                    if self._coordinate_overlay_web_fallback(overlay):
                        return
                    text, color = _format_supervisor_game_overlay_status(overlay)
                    self._set_overlay_status_summary(text, color)
                return
            self._kick_game_overlay_watchdog()
            if self.service_manager is None:
                return
            snapshot = self.service_manager.get_status_snapshot()
            sidecar = snapshot.get("vision_sidecar") if isinstance(snapshot.get("vision_sidecar"), dict) else {}
            event = snapshot.get("overlay_event") if isinstance(snapshot.get("overlay_event"), dict) else {}
            host_visibility = snapshot.get("overlay_visibility") if isinstance(snapshot.get("overlay_visibility"), dict) else {}
            watchdog = snapshot.get("overlay_watchdog") if isinstance(snapshot.get("overlay_watchdog"), dict) else {}
            sidecar_status = str(sidecar.get("status") or "").strip()
            event_active = bool(event.get("active"))
            should_report = overlay_enabled or sidecar_status == "running" or event_active
            if should_report:
                host_reason = str(host_visibility.get("reason") or "").strip() if bool(host_visibility.get("ok")) else ""
                reason = _format_game_overlay_host_reason(host_reason) if host_reason else ("选择窗口活跃" if event_active else "等待选择")
                sidecar_text = "识别运行" if sidecar_status == "running" else "识别待机"
                watchdog_action = str(watchdog.get("last_action") or "").strip()
                if watchdog_action == "start_missing_process":
                    sidecar_text = "识别已自愈"
                elif watchdog_action == "error":
                    sidecar_text = "识别异常"
                color = UI_COLORS["green"] if bool(host_visibility.get("visible")) or event_active else UI_COLORS["warn"]
                build_id = str(
                    host_visibility.get("build_id") or event.get("build_id") or sidecar.get("build_id") or ""
                ).strip()
                build_suffix = f" / 构建 {build_id[:18]}" if build_id else ""
                self._set_overlay_status_summary(
                    f"游戏内显示: {reason} / {sidecar_text}{build_suffix}",
                    color,
                )
        except Exception:
            logger.debug("读取游戏内 overlay 状态失败。", exc_info=True)
        finally:
            if not self.stop_event.is_set():
                self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)
