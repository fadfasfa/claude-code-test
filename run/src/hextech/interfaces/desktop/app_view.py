"""Desktop DesktopViewMixin 职责模块。"""
# ruff: noqa: F403, F405

from hextech.interfaces.desktop.app_shared import *


class DesktopViewMixin:
    def _run_on_ui_thread(self, callback):
        root = getattr(self, "root", None)
        if root is None:
            return False
        try:
            root.after(0, callback)
            return True
        except tk.TclError:
            return False

    def _set_window_topmost(self, enabled: bool) -> None:
        if self._window_topmost == enabled:
            return
        try:
            self.root.attributes("-topmost", enabled)
            if enabled:
                self.root.lift()
            self._window_topmost = enabled
        except tk.TclError:
            logger.debug("切换窗口置顶状态失败。", exc_info=True)

    def _show_overlay(self, topmost: bool = True) -> None:
        try:
            self.root.deiconify()
            self._set_window_topmost(topmost)
            self.root.update_idletasks()
            self._window_visible = True
        except tk.TclError:
            logger.debug("显示悬浮窗失败。", exc_info=True)

    def _hide_overlay(self) -> None:
        try:
            self._set_window_topmost(False)
            self.root.withdraw()
            self._window_visible = False
        except tk.TclError:
            logger.debug("隐藏悬浮窗失败。", exc_info=True)

    def _reload_data_into_ui(self, status_text, status_color):
        new_champions = self.load_data()

        def _update_on_main():
            with self._champions_lock:
                self.champions = new_champions
            self._set_status(status_text, status_color)

        if not self._run_on_ui_thread(_update_on_main):
            with self._champions_lock:
                self.champions = new_champions

    def load_data(self):
        if self._snapshot_client is None:
            return _empty_champions()
        try:
            snapshot_view = self._snapshot_client.open_view()
        except Exception:
            return _empty_champions()
        self._snapshot_generation_id = str(snapshot_view.status().get("generation_id") or "")
        champions = snapshot_view.get_champions()
        if not champions:
            return _empty_champions()
        return champions

    def _snapshot_watch_loop(self) -> None:
        """监视 DataService 原子指针，首代或新代发布后刷新桌面列表。"""

        while not self.stop_event.wait(1.0):
            if self._snapshot_client is None:
                continue
            status = self._snapshot_client.status()
            generation_id = str(status.get("generation_id") or "")
            if not generation_id or generation_id == self._snapshot_generation_id:
                continue
            new_champions = self.load_data()
            if not new_champions:
                continue
            with self._champions_lock:
                self.champions = new_champions

            def refresh_ui() -> None:
                degraded = [str(item) for item in status.get("degraded_sources") or []]
                short_generation = generation_id[:18]
                if degraded:
                    self._set_status(
                        f"数据已更新 {short_generation} · 沿用: {', '.join(degraded)}",
                        UI_COLORS["warn"],
                    )
                else:
                    self._set_status(f"数据已更新 {short_generation}", UI_COLORS["green"])
                self.update_ui(self.current_candidate_groups)

            self._run_on_ui_thread(refresh_ui)

    def on_hero_click(self, champ_id, hero_name):
        """处理英雄卡片点击，并触发终端输出与页面跳转。"""

        ui_runtime.handle_hero_click(self, champ_id, hero_name)

    def lcu_polling_loop(self):
        ui_runtime.lcu_polling_loop(self)

    def _load_and_set_img(self, champ_id, label):
        ui_runtime.load_and_set_img(self, champ_id, label)

    def _candidate_groups_from_input(self, hero_ids) -> dict[str, list[str]]:
        from hextech.contracts.identifiers import optional_champion_id

        def normalized(values) -> list[str]:
            return [str(result) for value in (values or []) if (result := optional_champion_id(value)) is not None]

        if isinstance(hero_ids, Mapping):
            selected = hero_ids.get("selected_champion_ids") or hero_ids.get("selected") or []
            bench = hero_ids.get("bench_champion_ids") or hero_ids.get("bench") or []
            return {
                "selected_champion_ids": normalized(selected),
                "bench_champion_ids": normalized(bench),
                "local_champion_id": str(optional_champion_id(hero_ids.get("local_champion_id")) or ""),
                "teammate_champion_ids": normalized(hero_ids.get("teammate_champion_ids", [])),
                "context_phase": str(hero_ids.get("context_phase") or ""),
                "context_connection_state": str(hero_ids.get("context_connection_state") or ""),
                "context_error_code": str(hero_ids.get("context_error_code") or ""),
            }
        values = list(hero_ids or [])
        return {
            "selected_champion_ids": [],
            "bench_champion_ids": normalized(values),
        }

    def _build_candidate_display_list(self, hero_ids, champions: list[dict]) -> list[dict]:
        import time

        from hextech.contracts import GameContext, GameSessionId
        from hextech.contracts.identifiers import optional_champion_id
        from hextech.modules.recommendation import RecommendationService

        candidate_groups = self._candidate_groups_from_input(hero_ids)
        local_id = candidate_groups.get("local_champion_id", "")
        teammate_ids = list(dict.fromkeys([
            *candidate_groups.get("teammate_champion_ids", []),
            *(value for value in candidate_groups.get("selected_champion_ids", []) if value != local_id),
        ]))
        context = GameContext(
            session_id=GameSessionId(str(getattr(self, "_client_session_id", "") or "desktop-session")),
            observed_at=time.time(),
            local_champion_id=optional_champion_id(local_id),
            teammate_champion_ids=tuple(
                value for item in teammate_ids if (value := optional_champion_id(item)) is not None
            ),
            bench_champion_ids=tuple(
                value
                for item in candidate_groups.get("bench_champion_ids", [])
                if (value := optional_champion_id(item)) is not None
            ),
            phase=str(candidate_groups.get("context_phase") or "champ_select"),
        )
        snapshot_client = getattr(self, "_snapshot_client", None)
        if snapshot_client is not None:
            snapshot_view = snapshot_client.open_view()
        else:
            # 只用于旧调用者和组件测试；生产 UI 始终从 DataSnapshotClient 打开固定代。
            class _RowsView:
                def status(self):
                    return {"state": "ready", "generation_id": "test-generation", "private_stats_enabled": True}

                def get_champion(self, champion_id):
                    needle = str(champion_id)
                    return next(
                        (
                            dict(row)
                            for row in champions
                            if str(optional_champion_id(row.get("id", row.get("英雄 ID", row.get("ID", "")))) or "")
                            == needle
                        ),
                        None,
                    )

                def get_champions(self):
                    return [dict(row) for row in champions]

            snapshot_view = _RowsView()
        recommendation = RecommendationService().build(context, snapshot_view)
        display_list: list[dict] = []
        for row in recommendation.champion_candidates:
            try:
                win = float(row.get("英雄胜率", row.get("胜率", row.get("win_rate", 0.5))))
            except (TypeError, ValueError):
                win = 0.5
            try:
                pick = float(row.get("英雄出场率", row.get("出场率", row.get("pick_rate", 0.1))))
            except (TypeError, ValueError):
                pick = 0.1
            display_list.append(
                {
                    "id": str(row.get("id") or ""),
                    "name": row.get("name", row.get("英雄名称", row.get("英雄名", "未知"))),
                    "win": win,
                    "pick": pick,
                    "tier": row.get("英雄评级", row.get("评级", "T?")),
                    "selection_role": row.get("selection_role", "bench"),
                }
            )
        return display_list

    def update_ui(self, hero_ids):
        if self._ui_render_in_progress:
            self._pending_ui_refresh = hero_ids
            return

        self._ui_render_in_progress = True
        try:
            for widget in self.list_frame.winfo_children():
                widget.destroy()

            with self._champions_lock:
                current_champions = list(self.champions)
                is_empty = not current_champions

            candidate_groups = self._candidate_groups_from_input(hero_ids)
            has_candidates = any(candidate_groups.get(key) for key in ("selected_champion_ids", "bench_champion_ids"))
            connection = str(candidate_groups.get("context_connection_state") or "")
            phase = str(candidate_groups.get("context_phase") or "")
            if not hero_ids or not has_candidates:
                if connection == "disconnected":
                    empty_message = "未连接客户端"
                elif phase == "champ_select":
                    empty_message = "当前没有备战席英雄"
                else:
                    empty_message = "尚未进入选人"
            elif is_empty:
                empty_message = "已取得英雄，统计快照加载中"
            else:
                empty_message = ""
            if empty_message:
                tk.Label(
                    self.list_frame,
                    text=empty_message,
                    fg=UI_COLORS["warn"],
                    bg=UI_COLORS["base"],
                    font=("Microsoft YaHei", 9),
                ).pack(pady=20)
                return

            if connection == "degraded":
                self.status_label.config(text="客户端连接暂时中断，保留最近选择", fg=UI_COLORS["warn"])
            else:
                self.status_label.config(text="实时数据已挂载", fg=UI_COLORS["green"])

            display_list = self._build_candidate_display_list(hero_ids, current_champions)
            if not display_list:
                tk.Label(
                    self.list_frame,
                    text="当前数据集中缺少对应英雄",
                    fg=UI_COLORS["warn"],
                    bg=UI_COLORS["base"],
                    font=("Microsoft YaHei", 9),
                ).pack(pady=20)
                return

            for item in display_list:
                role = item.get("selection_role", "bench")
                role_style = _selection_role_style(str(role))
                role_color = str(role_style["accent"])
                card_surface = str(role_style["surface"])
                card = tk.Frame(
                    self.list_frame,
                    bg=card_surface,
                    highlightthickness=int(role_style["border_width"]),
                    highlightbackground=role_color,
                    pady=3,
                    padx=4,
                    cursor="hand2",
                )
                card.pack(fill=tk.X, pady=3, padx=(0, 10))

                if int(role_style["marker_width"]) > 0:
                    tk.Frame(card, bg=role_color, width=int(role_style["marker_width"])).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))

                ribbon_color = UI_COLORS["green"] if item["win"] >= 0.5 else UI_COLORS["red"]
                ribbon = tk.Frame(card, bg=ribbon_color, width=3)
                ribbon.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))

                img_label = tk.Label(
                    card,
                    bg=card_surface,
                    highlightthickness=2 if role in {"self", "teammate"} else 1,
                    highlightbackground=role_color if role in {"self", "teammate"} else UI_COLORS["gold"],
                )
                img_label.pack(side=tk.LEFT, padx=(0, 8))
                threading.Thread(
                    target=lambda champion_id=item["id"], label=img_label: self._load_and_set_img(champion_id, label),
                    daemon=True,
                ).start()

                # 折叠态只渲染头像 + T 级标签，省掉胜率/出场率/胜率条
                if self._collapsed:
                    tk.Label(
                        card,
                        text=item["tier"],
                        font=("Microsoft YaHei", 9, "bold"),
                        fg=UI_COLORS["text"],
                        bg=card_surface,
                    ).pack(side=tk.LEFT)
                    if role in {"self", "teammate"}:
                        tk.Label(
                            card,
                            text="我" if role == "self" else "队友",
                            font=("Microsoft YaHei", 7, "bold"),
                            fg=str(role_style["text"]),
                            bg=role_color,
                            padx=5 if role == "self" else 3,
                            pady=1,
                        ).pack(side=tk.RIGHT)

                    def bind_collapsed_click(widget, cid, name):
                        widget.bind("<Button-1>", lambda e, c=cid, n=name: self.on_hero_click(c, n))
                        for child in widget.winfo_children():
                            bind_collapsed_click(child, cid, name)

                    bind_collapsed_click(card, item["id"], item["name"])
                    continue

                info = tk.Frame(card, bg=card_surface)
                info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

                title = self.core_data.get(str(item["id"]), {}).get("title", "")
                full_name = f"{item['name']} {title}".strip() if title else item["name"]

                title_row = tk.Frame(info, bg=card_surface)
                title_row.pack(fill=tk.X)
                tk.Label(
                    title_row,
                    text=f"[{item['tier']}] {full_name}",
                    font=("Microsoft YaHei", 9, "bold"),
                    fg=UI_COLORS["text"],
                    bg=card_surface,
                ).pack(side=tk.LEFT, anchor="w")
                if role in {"self", "teammate"}:
                    tk.Label(
                        title_row,
                        text="我的英雄" if role == "self" else "队友已选",
                        font=("Microsoft YaHei", 8, "bold"),
                        fg=str(role_style["text"]),
                        bg=role_color,
                        padx=7 if role == "self" else 5,
                        pady=1,
                    ).pack(side=tk.RIGHT)
                tk.Label(
                    info,
                    text=f"胜率: {item['win']:.1%} | 出场: {item['pick']:.1%}",
                    font=("Microsoft YaHei", 8),
                    fg=UI_COLORS["muted"],
                    bg=card_surface,
                ).pack(anchor="w", pady=(1, 0))

                bar_canvas = tk.Canvas(info, height=3, bg=UI_COLORS["base"], highlightthickness=0)
                bar_canvas.pack(fill=tk.X, pady=(3, 0))
                ratio = max(0, min(1, (item["win"] - 0.40) / 0.20))

                # 渐变填充 + 50% 温饱基准线，替代纯红黄绿三色阈值
                bar_canvas.bind(
                    "<Configure>",
                    lambda e, c=bar_canvas, r=ratio: _render_winrate_bar(c, e.width, r),
                )

                def bind_click(widget, cid, name):
                    widget.bind("<Button-1>", lambda e, c=cid, n=name: self.on_hero_click(c, n))
                    for child in widget.winfo_children():
                        bind_click(child, cid, name)

                bind_click(card, item["id"], item["name"])
        finally:
            self._ui_render_in_progress = False
            if self._pending_ui_refresh is not None:
                pending = self._pending_ui_refresh
                self._pending_ui_refresh = None
                self.root.after_idle(lambda ids=pending: self.update_ui(ids))

    def window_sync_loop(self):
        ui_runtime.window_sync_loop(self)

    def start_move(self, event):
        self.x, self.y = event.x, event.y
        self._auto_follow_enabled = False
        self._manual_move_timestamp = time.time()
        # 在状态栏给出明确的"已挂起自动对齐"提示，避免玩家误以为跟随失灵
        self._set_status("[手动] 自动对齐已挂起 8s", "#f9e2af")

    def do_move(self, event):
        next_x = self.root.winfo_x() + (event.x - self.x)
        next_y = self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{next_x}+{next_y}")
        self._last_overlay_target_pos = (next_x, next_y)
        self._manual_move_timestamp = time.time()


    def _move_overlay_to(self, x: int, y: int) -> None:
        try:
            current_pos = (self.root.winfo_x(), self.root.winfo_y())
            target_pos = (int(x), int(y))
            if current_pos == target_pos:
                return
            self.root.geometry(f"+{target_pos[0]}+{target_pos[1]}")
            self._last_overlay_target_pos = target_pos
        except tk.TclError:
            logger.debug("更新悬浮窗位置失败。", exc_info=True)

    def _resume_auto_follow(self) -> None:
        was_manual = not self._auto_follow_enabled
        self._auto_follow_enabled = True
        self._manual_move_timestamp = 0.0
        self._last_client_rect = None
        # 恢复自动跟随后把状态栏回写成"实时数据"基线文案，避免一直停留在挂起提示
        if was_manual:
            self._run_on_ui_thread(lambda: self._set_status("实时数据已挂载", UI_COLORS["green"]))

    def _toggle_collapse(self, _event=None) -> None:
        """切换悬浮窗折叠态：展开 320 px / 折叠 80 px。"""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.root.geometry(WINDOW_COLLAPSED_GEOMETRY)
            # 折叠态隐藏底部状态栏，保留头像列表本体
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.pack_forget()
            if hasattr(self, "overlay_status_label") and self.overlay_status_label.winfo_exists():
                self.overlay_status_label.pack_forget()
        else:
            self.root.geometry(WINDOW_EXPANDED_GEOMETRY)
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.pack(side=tk.BOTTOM, pady=5)
            if hasattr(self, "overlay_status_label") and self.overlay_status_label.winfo_exists():
                self.overlay_status_label.pack(side=tk.BOTTOM, pady=(0, 2))
        self._schedule_current_hero_refresh()

    def _schedule_current_hero_refresh(self) -> None:
        """合并快速折叠/展开触发的重复渲染，降低头像异步加载竞态。"""
        if self._collapse_render_after_id is not None:
            try:
                self.root.after_cancel(self._collapse_render_after_id)
            except tk.TclError:
                logger.debug("取消折叠态重渲染失败。", exc_info=True)
        self._collapse_render_after_id = self.root.after(60, self._refresh_current_hero_ids)

    def _refresh_current_hero_ids(self) -> None:
        self._collapse_render_after_id = None
        self.update_ui(self.current_candidate_groups)

    def _manual_follow_cooldown_elapsed(self, cooldown_seconds: float) -> bool:
        if self._manual_move_timestamp <= 0:
            return True
        return (time.time() - self._manual_move_timestamp) >= cooldown_seconds

    def _restore_from_terminal(self):
        self.pause_event.clear()
        self._show_overlay(topmost=True)

    def start_background_scraper(self):
        """启动 generation 只读 watcher；抓取和发布仍只由 DataService 执行。"""

        if self._snapshot_watch_started:
            return
        self._snapshot_watch_started = True
        self._start_tracked_thread(self._snapshot_watch_loop, name="hextech-desktop-snapshot-watch")

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        if hasattr(self, "exit_button"):
            try:
                self.exit_button.config(state=tk.DISABLED)
            except tk.TclError:
                logger.debug("禁用快速退出按钮失败。", exc_info=True)
        print("\n[System] 收到快速退出信号。")
        self.stop_event.set()
        if self._overlay_status_after_id is not None:
            try:
                self.root.after_cancel(self._overlay_status_after_id)
                self._overlay_status_after_id = None
            except tk.TclError:
                logger.debug("取消 overlay 状态轮询失败。", exc_info=True)
        self._supervisor_lease_stop.set()
        try:
            self.root.withdraw()
            self.root.update_idletasks()
        except tk.TclError:
            logger.debug("快速退出隐藏窗口失败。", exc_info=True)

        def cleanup() -> None:
            deadline = time.monotonic() + 7.5
            try:
                with self._threads_lock:
                    threads = list(self.threads)
                for thread in threads:
                    if thread is threading.current_thread() or not thread.is_alive():
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    thread.join(timeout=min(0.2, remaining))
                ui_runtime.close_companion_browser()
                ui_runtime.shutdown_desktop_executors(wait=False)
                service_manager = self._take_service_manager_for_shutdown()
                if service_manager is not None:
                    try:
                        service_manager.shutdown(timeout_seconds=0.5, final_timeout_seconds=4.5)
                    finally:
                        with self._service_manager_lock:
                            if self._service_manager_shutdown_in_progress is service_manager:
                                self._service_manager_shutdown_in_progress = None
                                self._service_manager_shutdown_completed = service_manager
                lease_thread = self._supervisor_lease_thread
                if lease_thread is not None and lease_thread.is_alive():
                    lease_thread.join(timeout=max(0.0, min(0.5, deadline - time.monotonic())))
                supervisor = self.runtime_supervisor
                if supervisor is not None and time.monotonic() < deadline:
                    supervisor.stop(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
                self.data_service = None
            except Exception:
                logger.exception("快速退出后台清理失败。")
            finally:
                self._shutdown_done_event.set()

        threading.Thread(target=cleanup, name="hextech-fast-exit", daemon=True).start()
        try:
            self.root.destroy()
        except tk.TclError:
            logger.debug("快速退出销毁窗口失败。", exc_info=True)

    def wait_for_shutdown(self, *, timeout_seconds: float = 8.0) -> bool:
        """主窗口消失后最多等待后台清理 8 秒，不再阻塞 Tk 主线程。"""

        return self._shutdown_done_event.wait(timeout=max(0.0, float(timeout_seconds)))
