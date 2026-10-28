"""Desktop DesktopControlsMixin 职责模块。"""
import time as _time

from hextech.interfaces.desktop.app_shared import (
    UI_COLORS,
    _format_runtime_game_overlay_status,
    data_refresh_status_signature,
    export_user_diagnostics,
    format_data_age_suffix,
    format_data_refresh_status,
    logger,
    log_data_refresh_result,
    save_ui_feature_flags,
    scaled,
    threading,
    tk,
)

# 状态行合成参数：service 消息（按钮反馈等）在 6 秒新鲜窗口内优先显示，
# 之后自然让位给 overlay 摘要；单行按 CJK 宽度预算截断（8pt 在 150% 下
# 约 16px/字，18 字 ≈ 288px，加状态点与边距不超过 320px 窗宽）。
STATUS_SERVICE_FRESH_SECONDS = 6.0
STATUS_LINE_MAX_CHARS = 18


class DesktopControlsMixin:
    def _build_ui(self):
        scale = self._ui_scale_value()
        self.title_frame = tk.Frame(self.root, bg=UI_COLORS["header"])
        self.title_frame.pack(fill=tk.X)

        self.title_bar = tk.Label(
            self.title_frame,
            text="备战席",
            bg=UI_COLORS["header"],
            fg=UI_COLORS["gold"],
            font=self._ui_font(16, bold=True),
            pady=scaled(8, scale),
        )
        self.title_bar.bind("<ButtonPress-1>", self.start_move)

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
            font=self._ui_font(15, bold=True),
            cursor="hand2",
        )
        # 右上角“×”只隐藏到托盘；完全退出必须使用托盘菜单，避免误杀识别进程。
        self.exit_button.pack(side=tk.RIGHT, padx=(0, 6), pady=5)

        self.diagnostics_button = tk.Button(
            self.title_frame,
            text="诊断",
            command=self._show_diagnostics_menu,
            bg=UI_COLORS["surface_alt"],
            fg=UI_COLORS["muted"],
            activebackground=UI_COLORS["surface"],
            activeforeground=UI_COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=scaled(8, scale),
            pady=scaled(3, scale),
            font=self._ui_font(11, bold=True),
            cursor="hand2",
        )
        self.diagnostics_button.pack(side=tk.RIGHT, padx=(0, 8), pady=6)

        self.refresh_button = tk.Button(
            self.title_frame,
            text="刷新",
            command=self._start_data_refresh,
            bg=UI_COLORS["surface_alt"],
            fg=UI_COLORS["cyan"],
            activebackground=UI_COLORS["surface"],
            activeforeground=UI_COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=scaled(8, scale),
            pady=scaled(3, scale),
            font=self._ui_font(11, bold=True),
            cursor="hand2",
        )
        self.refresh_button.pack(side=tk.RIGHT, padx=(0, 6), pady=6)
        # 标题文本最后 pack：空间不足时优先保住右侧诊断与关闭按钮。
        self.title_bar.pack(side=tk.LEFT, padx=(scaled(10, scale), 0))

        self.feature_frame = tk.Frame(self.root, bg=UI_COLORS["base"], padx=6, pady=6)
        self.feature_frame.pack(fill=tk.X)
        self._feature_toggle_widgets = []
        self.game_overlay_var = tk.BooleanVar(value=self.feature_flags["game_overlay_enabled"])
        self.private_stats_var = tk.BooleanVar(value=self.feature_flags["private_policy_stats_enabled"])
        self.low_frequency_listener_var = tk.BooleanVar(value=self.feature_flags["low_frequency_listener_enabled"])

        self.game_overlay_check = self._build_feature_toggle(
            "游戏内显示",
            self.game_overlay_var,
            self._toggle_game_overlay,
            accent=UI_COLORS["cyan"],
        )
        self.game_overlay_check.grid(row=0, column=0, sticky="ew", padx=2)
        self.private_stats_check = self._build_feature_toggle(
            "私用统计",
            self.private_stats_var,
            self._toggle_private_policy_stats,
            accent=UI_COLORS["green"],
        )
        self.private_stats_check.grid(row=0, column=1, sticky="ew", padx=(2, 0))

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
        # 滚动条不在此处 pack：只在内容真正溢出可视高度时由 _sync_list_scrollbar
        # 显示，未溢出时常驻的滑块会破坏观感（真机反馈）。
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._list_window_id = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        # 卡片宽度必须跟随 canvas 实际宽度，否则 fill=X 只能撑到内容自然宽度
        self.canvas.bind(
            "<Configure>",
            lambda e: (
                self.canvas.itemconfigure(self._list_window_id, width=e.width),
                self._sync_list_scrollbar(),
            ),
        )

        def _on_mousewheel(event):
            # 滚轮只在指针位于列表区域内时生效，不再全局拦截整个应用的滚轮；
            # 内容未溢出（滚动条隐藏）时不响应，避免无意义的视图抖动。
            node = self.root.winfo_containing(event.x_root, event.y_root)
            while node is not None:
                if node is self.list_shell:
                    if self.list_scrollbar.winfo_ismapped():
                        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                    break
                node = getattr(node, "master", None)

        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        self.list_frame.bind(
            "<Configure>",
            lambda e: (
                self.canvas.configure(scrollregion=self.canvas.bbox("all")),
                self._sync_list_scrollbar(),
            ),
        )

        # 底部单行状态栏：状态点 + 关键短语。旧结构是两条独立 Label，320px 宽度
        # 下长文案（构建号、诊断路径）必然被裁切；收敛为单行后由
        # _render_status_line 按通道优先级合成，细节移入日志与诊断导出。
        # 点 + 文字放进内层 frame 整体水平居中（真机反馈：左对齐观感差）。
        # 外层 padx 必须对称：非对称值会让内层居中相对窗口真实中心产生偏移。
        self.status_bar = tk.Frame(self.root, bg=UI_COLORS["base"])
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 6))
        self._status_bar_inner = tk.Frame(self.status_bar, bg=UI_COLORS["base"])
        self._status_bar_inner.pack()
        self.status_dot = tk.Canvas(
            self._status_bar_inner,
            width=10,
            height=10,
            bg=UI_COLORS["base"],
            highlightthickness=0,
            bd=0,
        )
        self.status_dot.pack(side=tk.LEFT, padx=(0, 5))
        self.status_line_label = tk.Label(
            self._status_bar_inner,
            text="系统初始化中...",
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=self._ui_font(11),
        )
        self.status_line_label.pack(side=tk.LEFT)
        self._render_status_line()
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
        frame = tk.Frame(self.feature_frame, bg=UI_COLORS["base"], cursor="hand2", padx=1, pady=2)
        dot = tk.Canvas(
            frame,
            width=scaled(13, scale),
            height=scaled(13, scale),
            bg=UI_COLORS["base"],
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        dot.pack(side=tk.LEFT, padx=(0, 3))
        label = tk.Label(
            frame,
            text=text,
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=self._ui_font(11, bold=True),
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
            if text in {"游戏内显示", "私用统计"} and not self._runtime_services_ready:
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

    def _show_diagnostics_menu(self) -> None:
        from hextech.interfaces.desktop.selection_diagnostics_view import show_selection_diagnostics_menu
        show_selection_diagnostics_menu(self.root, self.diagnostics_button,
            export_diagnostics=self._start_user_diagnostics_export, set_status=self._set_status)

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
                zip_path = str(result.zip_path)
                # 完整路径进剪贴板与日志：320px 单行状态栏装不下长路径。
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(zip_path)
                except tk.TclError:
                    logger.debug("诊断路径写入剪贴板失败。", exc_info=True)
                logger.info("诊断已导出: %s", zip_path)
                self._set_status("诊断已导出 · 路径已复制", UI_COLORS["green"])

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-user-diagnostics-export")

    def _collect_feature_flags_from_controls(self) -> dict:
        return {
            "game_overlay_enabled": bool(self.game_overlay_var.get()),
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
        # 后台已完成服务初始化；这里仅同步依赖 Tk 的控件状态。
        # 游戏内显示依赖 App runtime owner；App owner 延后到首屏绘制后启动，
        # 所以持久化恢复必须等控制面就绪后单独执行。
        self._game_overlay_desired_enabled = bool(self.feature_flags.get("game_overlay_enabled"))

    def _restore_persisted_game_overlay(self) -> None:
        if self.runtime_owner is None:
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


    def _toggle_game_overlay(self) -> None:
        toggle_name = "游戏内显示"
        enabled = bool(self.game_overlay_var.get())
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
                    if self.runtime_owner is None:
                        raise RuntimeError("App runtime owner 未启动")
                    action = self.runtime_owner.set_game_overlay_enabled(enabled)
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
        """启动时只回显 DataService 状态；自动调度仍由服务端独占。"""

        self._start_tracked_thread(
            self._poll_data_refresh_status_once,
            name="hextech-data-refresh-status-initial",
        )

    def _start_data_refresh(self) -> None:
        """请求核心来源强刷；重复点击由 DataService 合并为一次 recheck。"""

        if self.data_service is None or not self._runtime_services_ready:
            self._set_status("数据服务仍在启动中", UI_COLORS["warn"])
            return
        self._set_data_refresh_status(
            {
                "state": "queued",
                "scope": "core",
                "phase": "core",
                "reason_code": "refresh_queued",
                "started_at": _time.time(),
                "completed_at": 0.0,
            }
        )

        def worker() -> None:
            try:
                accepted = self.data_service.refresh(scope="core", force=True)
                if not accepted.get("accepted"):
                    raise RuntimeError(str(accepted.get("reason_code") or "刷新请求未受理"))
                self._poll_data_refresh_status_once()
            except Exception:
                logger.warning("手动数据刷新请求失败。", exc_info=True)
                self._run_on_ui_thread(
                    lambda: self._set_data_refresh_status(
                        {
                            "state": "failed",
                            "scope": "core",
                            "phase": "core",
                            "reason_code": "refresh_request_failed",
                            "started_at": 0.0,
                            "completed_at": _time.time(),
                        }
                    )
                )

        self._start_tracked_thread(worker, name="hextech-manual-data-refresh")

    def _poll_data_refresh_status_once(self) -> None:
        handle = self.data_service
        if handle is None:
            return
        try:
            payload = handle.get_status()
            refresh_status = payload.get("refresh_status")
            if not isinstance(refresh_status, dict):
                return
        except Exception:
            logger.debug("读取 DataService 刷新状态失败。", exc_info=True)
            return
        self._run_on_ui_thread(
            lambda status=dict(refresh_status): self._set_data_refresh_status(status)
        )

    def _set_data_refresh_status(self, status: dict) -> None:
        channels = getattr(self, "_status_channels", None)
        if channels is None:
            return
        text, color = format_data_refresh_status(status)
        state = str(status.get("state") or "idle")
        signature = data_refresh_status_signature(status)
        previous = channels.get("refresh") or {}
        if previous.get("signature") != signature:
            at = _time.monotonic()
            completed_at = float(status.get("completed_at") or 0.0)
            if state in {"completed", "unchanged", "failed"} and completed_at > 0:
                age = max(0.0, _time.time() - completed_at)
                at -= age
            channels["refresh"] = {
                "text": text,
                "color": color,
                "state": state,
                "at": at,
                "signature": signature,
            }
            log_data_refresh_result(status)
        self._render_status_line()

    def _set_status(self, text, color):
        """service 通道写入：调用方语义不变，渲染收敛到单行状态栏。"""

        channels = getattr(self, "_status_channels", None)
        if channels is None:
            return
        channels["service"] = {"text": str(text or ""), "color": color, "at": _time.monotonic()}
        self._render_status_line()

    def _set_overlay_status_summary(self, text: str, color: str) -> None:
        """overlay 通道写入：只更新游戏内显示摘要，不覆盖 service 通道。"""

        self._overlay_status_text = str(text or "")
        self._overlay_status_color = color
        channels = getattr(self, "_status_channels", None)
        if channels is None:
            return
        channels["overlay"] = {"text": self._overlay_status_text, "color": color, "at": _time.monotonic()}
        self._render_status_line()

    def _render_status_line(self) -> None:
        """合成单行状态：error 置顶 → service 新鲜窗口 → overlay → 回落 service。

        时效后缀只在整行放得下时追加（保住主状态不被截断）；widget 缺失时
        静默跳过，兼容测试里的轻量伪对象。
        """

        channels = getattr(self, "_status_channels", None)
        if channels is None:
            return
        service = channels["service"]
        overlay = channels["overlay"]
        refresh = channels.get("refresh") or {}
        now = _time.monotonic()
        if service["text"] and service["color"] == UI_COLORS["error"]:
            chosen = service
        elif refresh.get("text") and (
            refresh.get("state") in {"queued", "running", "deferred"}
            or now - float(refresh.get("at") or 0.0) < STATUS_SERVICE_FRESH_SECONDS
        ):
            chosen = refresh
        elif service["text"] and now - float(service["at"]) < STATUS_SERVICE_FRESH_SECONDS:
            chosen = service
        elif overlay["text"]:
            chosen = overlay
        else:
            chosen = service
        text = str(chosen["text"] or "")
        color = chosen["color"]
        if color != UI_COLORS["error"] and chosen is not refresh:
            suffix = format_data_age_suffix(getattr(self, "_data_created_ts", 0.0), _time.time())
            if suffix and (hasattr(getattr(self, "status_line_label", None), "tk") or len(text) + len(suffix) <= STATUS_LINE_MAX_CHARS):
                text += suffix
        self._status_full_text = text
        if hasattr(getattr(self, "status_line_label", None), "tk"):
            from tkinter.font import Font
            from .responsive_view import wrap_text
            font = Font(root=self.root, font=self._ui_font(11))
            available = max(1, int(getattr(self, "_overlay_pixel_width", 320)) - 36)
            text = wrap_text(text, font, available)
        elif len(text) > STATUS_LINE_MAX_CHARS:
            text = text[: STATUS_LINE_MAX_CHARS - 1] + "…"
        label = getattr(self, "status_line_label", None)
        if label is not None and label.winfo_exists():
            label.config(text=text, fg=color)
        dot = getattr(self, "status_dot", None)
        if dot is not None and dot.winfo_exists():
            try:
                dot.delete("all")
                dot.create_oval(2, 2, 9, 9, fill=color, outline=color)
            except tk.TclError:
                logger.debug("刷新状态点失败。", exc_info=True)

    def _start_overlay_status_polling(self) -> None:
        self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)


    def _refresh_overlay_status_summary(self) -> None:
        """低频回显游戏内 overlay 状态，避免 running 但不可见时没有反馈。"""

        try:
            runtime_state = str(getattr(self, "_background_runtime_state", "running"))
            if runtime_state != "running":
                text = {
                    "suspending": "正在进入轻量待机",
                    "suspended": "识别已休眠",
                    "resuming": "识别恢复中",
                    "restart_in_progress": "识别重启中",
                    "resume_failed": "识别恢复失败",
                }.get(runtime_state, "后台状态未知")
                self._set_overlay_status_summary(text, UI_COLORS["warn"])
                return
            overlay_enabled = bool(self.game_overlay_var.get())
            if self.runtime_owner is not None:
                snapshot = self.runtime_owner.get_status()
                components = snapshot.get("components") if isinstance(snapshot.get("components"), dict) else {}
                overlay = components.get("game_overlay") if isinstance(components.get("game_overlay"), dict) else {}
                should_report = overlay_enabled or str(overlay.get("status") or "") in {"starting", "running", "stopping", "error"}
                if should_report:
                    text, color = _format_runtime_game_overlay_status(overlay)
                    self._set_overlay_status_summary(text, color)
                return
            if overlay_enabled:
                self._set_overlay_status_summary("识别控制面准备中", UI_COLORS["warn"])
        except Exception:
            logger.debug("读取游戏内 overlay 状态失败。", exc_info=True)
        finally:
            if not self.stop_event.is_set():
                self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)
