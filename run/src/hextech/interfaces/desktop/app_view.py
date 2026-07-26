"""Desktop DesktopViewMixin 职责模块。"""
from hextech.interfaces.desktop.app_shared import (
    Mapping,
    TIER_COLORS,
    UI_COLORS,
    WINDOW_COLLAPSED_WIDTH,
    WINDOW_EXPANDED_WIDTH,
    _empty_champions,
    _render_winrate_bar,
    _selection_role_style,
    logger,
    scaled,
    threading,
    time,
    tk,
    ui_font,
    ui_runtime,
)
from hextech.modules.data.catalog.champion_tier import normalized_champion_tier


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
                    # 新 generation 已带权威评级；旧快照按同一 Python 规则回退，
                    # 因而不会向用户暴露含义不明的 T?。
                    "tier": normalized_champion_tier(
                        row.get("英雄评级", row.get("评级")),
                        score=row.get("综合分数"),
                    ),
                    "selection_role": row.get("selection_role", "bench"),
                }
            )
        return display_list

    def _ui_scale_value(self) -> float:
        return float(getattr(self, "_ui_scale", 1.0))

    def _ensure_card_state(self) -> None:
        """兼容轻量测试对象：惰性补齐 keyed 渲染缓存。"""

        if not hasattr(self, "_card_rows"):
            self._card_rows = {}
        if not hasattr(self, "_card_order"):
            self._card_order = []
        if not hasattr(self, "_list_placeholder"):
            self._list_placeholder = None

    def _reset_card_cache(self) -> None:
        self._ensure_card_state()
        for row in self._card_rows.values():
            try:
                row["card"].destroy()
            except tk.TclError:
                logger.debug("销毁候选卡片失败。", exc_info=True)
        self._card_rows = {}
        self._card_order = []

    def _clear_list_placeholder(self) -> None:
        if self._list_placeholder is not None:
            try:
                self._list_placeholder.destroy()
            except tk.TclError:
                logger.debug("销毁空态提示失败。", exc_info=True)
            self._list_placeholder = None

    def _show_list_placeholder(self, text: str) -> None:
        scale = self._ui_scale_value()
        self._reset_card_cache()
        self._clear_list_placeholder()
        self._list_placeholder = tk.Label(
            self.list_frame,
            text=text,
            fg=UI_COLORS["warn"],
            bg=UI_COLORS["base"],
            font=ui_font(scale, 12),
        )
        self._list_placeholder.pack(pady=scaled(20, scale))

    def _avatar_placeholder_image(self):
        """共享的圆角头像占位图：卡片首绘即占位，异步加载完成后原地替换。"""

        if getattr(self, "_avatar_placeholder_photo", None) is None:
            from PIL import Image, ImageDraw, ImageTk

            scale = self._ui_scale_value()
            size = scaled(48, scale)
            radius = scaled(8, scale)
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=UI_COLORS["surface_alt"])
            self._avatar_placeholder_photo = ImageTk.PhotoImage(img)
        return self._avatar_placeholder_photo

    def _tier_badge_style(self, tier: str) -> dict:
        return TIER_COLORS.get(str(tier or "").upper(), TIER_COLORS["T3"])

    def update_ui(self, hero_ids):
        if self._ui_render_in_progress:
            self._pending_ui_refresh = hero_ids
            return

        self._ui_render_in_progress = True
        try:
            self._ensure_card_state()
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
                self._show_list_placeholder(empty_message)
                return

            if connection == "degraded":
                self.status_label.config(text="客户端连接暂时中断，保留最近选择", fg=UI_COLORS["warn"])
            else:
                self.status_label.config(text="实时数据已挂载", fg=UI_COLORS["green"])

            display_list = self._build_candidate_display_list(hero_ids, current_champions)
            if not display_list:
                self._show_list_placeholder("当前数据集中缺少对应英雄")
                return

            self._clear_list_placeholder()
            self._render_candidate_cards(display_list)
        finally:
            self._ui_render_in_progress = False
            if self._pending_ui_refresh is not None:
                pending = self._pending_ui_refresh
                self._pending_ui_refresh = None
                self.root.after_idle(lambda ids=pending: self.update_ui(ids))

    def _render_candidate_cards(self, display_list: list[dict]) -> None:
        """keyed 增量渲染：同键行原地更新，成员变化才建/销卡片，消除全量重建闪烁。"""

        scale = self._ui_scale_value()
        desired_keys = [
            (str(item["id"]), str(item.get("selection_role", "bench")))
            for item in display_list
        ]
        desired_set = set(desired_keys)

        for key in list(self._card_rows):
            if key not in desired_set:
                row = self._card_rows.pop(key)
                try:
                    row["card"].destroy()
                except tk.TclError:
                    logger.debug("销毁移除的候选卡片失败。", exc_info=True)

        for item, key in zip(display_list, desired_keys):
            row = self._card_rows.get(key)
            if row is None:
                self._card_rows[key] = self._build_candidate_card(item, scale)
            else:
                self._update_candidate_card(row, item, scale)

        if desired_keys != self._card_order:
            for key in desired_keys:
                card = self._card_rows[key]["card"]
                card.pack_forget()
                card.pack(fill=tk.X, pady=scaled(3, scale), padx=(0, scaled(10, scale)))
            self._card_order = desired_keys

    def _build_candidate_card(self, item: dict, scale: float) -> dict:
        role = str(item.get("selection_role", "bench"))
        role_style = _selection_role_style(role)
        role_color = str(role_style["accent"])
        card_surface = str(role_style["surface"])
        badge_style = self._tier_badge_style(item["tier"])

        row: dict = {"id": item["id"], "name": item["name"], "tier": item["tier"], "win": None, "pick": None}
        card = tk.Frame(
            self.list_frame,
            bg=card_surface,
            highlightthickness=int(role_style["border_width"]),
            highlightbackground=role_color,
            pady=scaled(3, scale),
            padx=scaled(4, scale),
            cursor="hand2",
        )
        card.pack(fill=tk.X, pady=scaled(3, scale), padx=(0, scaled(10, scale)))
        row["card"] = card

        if int(role_style["marker_width"]) > 0:
            tk.Frame(card, bg=role_color, width=scaled(int(role_style["marker_width"]), scale)).pack(
                side=tk.LEFT, fill=tk.Y, padx=(0, scaled(4, scale))
            )

        ribbon = tk.Frame(card, bg=UI_COLORS["green"], width=scaled(3, scale))
        ribbon.pack(side=tk.LEFT, fill=tk.Y, padx=(0, scaled(5, scale)))
        row["ribbon"] = ribbon

        img_label = tk.Label(
            card,
            bg=card_surface,
            image=self._avatar_placeholder_image(),
            highlightthickness=scaled(2, scale) if role in {"self", "teammate"} else scaled(1, scale),
            highlightbackground=role_color if role in {"self", "teammate"} else UI_COLORS["gold"],
        )
        img_label.pack(side=tk.LEFT, padx=(0, scaled(8, scale)))
        threading.Thread(
            target=lambda champion_id=item["id"], label=img_label: self._load_and_set_img(champion_id, label),
            daemon=True,
        ).start()

        # 折叠态只渲染头像 + T 级徽章，省掉名称/胜率/出场率/胜率条
        if self._collapsed:
            badge = tk.Label(
                card,
                text=item["tier"],
                font=ui_font(scale, 12, bold=True),
                fg=badge_style["fg"],
                bg=badge_style["bg"],
                padx=scaled(3, scale),
            )
            badge.pack(side=tk.LEFT)
            row["tier_badge"] = badge
            if role in {"self", "teammate"}:
                tk.Label(
                    card,
                    text="我" if role == "self" else "队友",
                    font=ui_font(scale, 9, bold=True),
                    fg=str(role_style["text"]),
                    bg=role_color,
                    padx=scaled(5 if role == "self" else 3, scale),
                    pady=1,
                ).pack(side=tk.RIGHT)
            self._bind_card_click(card, row)
            self._update_candidate_card(row, item, scale)
            return row

        info = tk.Frame(card, bg=card_surface)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        title_row = tk.Frame(info, bg=card_surface)
        title_row.pack(fill=tk.X)
        badge = tk.Label(
            title_row,
            text=item["tier"],
            font=ui_font(scale, 11, bold=True),
            fg=badge_style["fg"],
            bg=badge_style["bg"],
            padx=scaled(4, scale),
        )
        badge.pack(side=tk.LEFT, padx=(0, scaled(4, scale)))
        row["tier_badge"] = badge
        name_label = tk.Label(
            title_row,
            text="",
            font=ui_font(scale, 12, bold=True),
            fg=UI_COLORS["text"],
            bg=card_surface,
        )
        name_label.pack(side=tk.LEFT, anchor="w")
        row["name_label"] = name_label
        if role in {"self", "teammate"}:
            tk.Label(
                title_row,
                text="我的英雄" if role == "self" else "队友已选",
                font=ui_font(scale, 11, bold=True),
                fg=str(role_style["text"]),
                bg=role_color,
                padx=scaled(7 if role == "self" else 5, scale),
                pady=1,
            ).pack(side=tk.RIGHT)
        stats_label = tk.Label(
            info,
            text="",
            font=ui_font(scale, 11),
            fg=UI_COLORS["muted"],
            bg=card_surface,
        )
        stats_label.pack(anchor="w", pady=(1, 0))
        row["stats_label"] = stats_label

        bar_canvas = tk.Canvas(info, height=scaled(3, scale), bg=UI_COLORS["base"], highlightthickness=0)
        bar_canvas.pack(fill=tk.X, pady=(scaled(3, scale), 0))
        row["bar_canvas"] = bar_canvas
        row["ratio"] = 0.0
        # 渐变填充 + 50% 温饱基准线；ratio 从 row 里读，原地更新后重绘即可
        bar_canvas.bind(
            "<Configure>",
            lambda e, c=bar_canvas, r=row: _render_winrate_bar(c, e.width, r.get("ratio", 0.0), height=scaled(4, scale)),
        )

        self._bind_card_click(card, row)
        self._update_candidate_card(row, item, scale)
        return row

    def _bind_card_click(self, widget, row: dict) -> None:
        widget.bind("<Button-1>", lambda e: self.on_hero_click(row["id"], row["name"]))
        for child in widget.winfo_children():
            self._bind_card_click(child, row)

    def _update_candidate_card(self, row: dict, item: dict, scale: float) -> None:
        """原地刷新可变字段（名称/评级/胜率），不重建 widget。"""

        row["id"] = item["id"]
        row["name"] = item["name"]
        try:
            if row.get("tier_badge") is not None and item["tier"] != row.get("tier"):
                badge_style = self._tier_badge_style(item["tier"])
                row["tier_badge"].config(text=item["tier"], fg=badge_style["fg"], bg=badge_style["bg"])
            row["tier"] = item["tier"]

            if row.get("name_label") is not None:
                title = self.core_data.get(str(item["id"]), {}).get("title", "")
                full_name = f"{item['name']} {title}".strip() if title else item["name"]
                if row["name_label"].cget("text") != full_name:
                    row["name_label"].config(text=full_name)

            if item["win"] != row.get("win") or item["pick"] != row.get("pick"):
                if row.get("stats_label") is not None:
                    row["stats_label"].config(text=f"胜率: {item['win']:.1%} | 出场: {item['pick']:.1%}")
                if row.get("ribbon") is not None:
                    row["ribbon"].config(bg=UI_COLORS["green"] if item["win"] >= 0.5 else UI_COLORS["red"])
                if row.get("bar_canvas") is not None:
                    row["ratio"] = max(0, min(1, (item["win"] - 0.40) / 0.20))
                    _render_winrate_bar(
                        row["bar_canvas"],
                        int(row["bar_canvas"].winfo_width()),
                        row["ratio"],
                        height=scaled(4, scale),
                    )
                row["win"] = item["win"]
                row["pick"] = item["pick"]
        except tk.TclError:
            logger.debug("原地更新候选卡片失败。", exc_info=True)

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
        """切换悬浮窗折叠态：展开/折叠只切宽度基值（乘 DPI），高度保持自适应值。"""
        self._collapsed = not self._collapsed
        scale = self._ui_scale_value()
        width_base = WINDOW_COLLAPSED_WIDTH if self._collapsed else WINDOW_EXPANDED_WIDTH
        self._overlay_pixel_width = scaled(width_base, scale)
        height = int(getattr(self, "_window_height_px", 740))
        self.root.geometry(f"{self._overlay_pixel_width}x{height}")
        # 折叠/展开布局不同，清空 keyed 缓存强制整批重建
        self._reset_card_cache()
        if hasattr(self, "collapse_button"):
            try:
                self.collapse_button.config(text="»" if self._collapsed else "«")
            except tk.TclError:
                logger.debug("更新折叠按钮文本失败。", exc_info=True)
        if self._collapsed:
            # 折叠态隐藏底部状态栏，保留头像列表本体
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.pack_forget()
            if hasattr(self, "overlay_status_label") and self.overlay_status_label.winfo_exists():
                self.overlay_status_label.pack_forget()
        else:
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

    def exit_application(self):
        """从托盘执行完全退出；右上角“×”不会进入此路径。"""

        if self._closing:
            return
        self._closing = True
        self.stop_background_runtime()
        if hasattr(self, "exit_button"):
            try:
                self.exit_button.config(state=tk.DISABLED)
            except tk.TclError:
                logger.debug("禁用快速退出按钮失败。", exc_info=True)
        logger.info("收到完全退出信号。")
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

    def on_close(self):
        """保留历史完全退出入口，供测试和非窗口关闭调用方兼容。"""

        self.exit_application()

    def wait_for_shutdown(self, *, timeout_seconds: float = 8.0) -> bool:
        """主窗口消失后最多等待后台清理 8 秒，不再阻塞 Tk 主线程。"""

        return self._shutdown_done_event.wait(timeout=max(0.0, float(timeout_seconds)))
