"""Desktop DesktopViewMixin 职责模块。"""
from hextech.interfaces.desktop.app_shared import (
    Mapping,
    TIER_COLORS,
    UI_COLORS,
    WINDOW_BASE_HEIGHT,
    _empty_champions,
    logger,
    parse_generation_created_ts,
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
        status = snapshot_view.status()
        self._snapshot_generation_id = str(status.get("generation_id") or "")
        # 顺带更新数据时效（供状态行"数据 X 前"后缀）；解析失败保留旧值不清零。
        created_ts = parse_generation_created_ts(status.get("created_at"))
        if created_ts > 0:
            self._data_created_ts = created_ts
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
                # generation 短号只进日志：320px 单行状态栏放不下且用户不消费。
                logger.info("数据已更新 %s，沿用来源: %s", generation_id, ", ".join(degraded) or "无")
                if degraded:
                    self._set_status("数据已更新 · 部分沿用旧源", UI_COLORS["warn"])
                else:
                    self._set_status("数据已更新", UI_COLORS["green"])
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
            font=ui_font(12),
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
                self._set_status("连接中断 · 保留最近选择", UI_COLORS["warn"])
            else:
                self._set_status("实时数据已挂载", UI_COLORS["green"])

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
        # 键只用英雄 id：bench→self 的角色跃迁只更新右侧状态位，不销毁重建卡片。
        desired_keys = [str(item["id"]) for item in display_list]
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
                card.pack(fill=tk.X, pady=scaled(2, scale), padx=(0, scaled(6, scale)))
            self._card_order = desired_keys

    def _build_candidate_card(self, item: dict, scale: float) -> dict:
        """构建紧凑英雄卡：强度在左、身份居中、角色与胜率固定在右。"""

        card_surface = UI_COLORS["surface"]
        badge_style = self._tier_badge_style(item["tier"])

        row: dict = {"id": item["id"], "name": item["name"], "tier": item["tier"], "win": None, "pick": None}
        card = tk.Frame(
            self.list_frame,
            bg=card_surface,
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
            pady=0,
            padx=0,
            cursor="hand2",
        )
        card.pack(fill=tk.X, pady=scaled(2, scale), padx=(0, scaled(6, scale)))
        row["card"] = card

        strength_bar = tk.Frame(
            card,
            width=scaled(6, scale),
            bg=badge_style["bg"],
            cursor="hand2",
        )
        strength_bar.pack(side=tk.LEFT, fill=tk.Y)
        row["strength_bar"] = strength_bar

        content = tk.Frame(
            card,
            bg=card_surface,
            padx=scaled(6, scale),
            pady=scaled(4, scale),
            cursor="hand2",
        )
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        img_label = tk.Label(
            content,
            bg=card_surface,
            image=self._avatar_placeholder_image(),
            bd=0,
            highlightthickness=scaled(1, scale),
            highlightbackground=UI_COLORS["gold"],
        )
        img_label.pack(side=tk.LEFT, padx=(0, scaled(7, scale)))
        row["img_label"] = img_label
        threading.Thread(
            target=lambda champion_id=item["id"], label=img_label: self._load_and_set_img(champion_id, label),
            daemon=True,
        ).start()

        # 固定指标列避免角色切换时胜率上下跳动；bench 使用同高空白状态位。
        metric = tk.Frame(content, width=scaled(72, scale), bg=card_surface, cursor="hand2")
        metric.pack(side=tk.RIGHT, fill=tk.Y)
        metric.pack_propagate(False)
        selected_badge = tk.Label(
            metric,
            text="",
            width=3,
            font=ui_font(11, bold=True),
            fg=card_surface,
            bg=card_surface,
            bd=0,
            padx=scaled(3, scale),
            pady=scaled(1, scale),
        )
        selected_badge.pack(anchor="ne")
        row["selected_badge"] = selected_badge
        row["selection_role"] = ""

        win_label = tk.Label(
            metric,
            text="",
            font=ui_font(16, bold=True),
            fg=UI_COLORS["green"],
            bg=card_surface,
            bd=0,
        )
        win_label.pack(side=tk.BOTTOM, anchor="e")
        row["win_label"] = win_label

        info = tk.Frame(content, bg=card_surface, cursor="hand2")
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        title_row = tk.Frame(info, bg=card_surface)
        title_row.pack(fill=tk.X)
        badge = tk.Label(
            title_row,
            text=item["tier"],
            font=ui_font(12, bold=True),
            fg=badge_style["fg"],
            bg=badge_style["bg"],
            bd=0,
            padx=scaled(4, scale),
        )
        badge.pack(side=tk.LEFT, padx=(0, scaled(4, scale)))
        row["tier_badge"] = badge
        name_label = tk.Label(
            title_row,
            text="",
            font=ui_font(12, bold=True),
            fg=UI_COLORS["text"],
            bg=card_surface,
            bd=0,
        )
        name_label.pack(side=tk.LEFT, anchor="w")
        row["name_label"] = name_label
        pick_label = tk.Label(
            info,
            text="",
            font=ui_font(11),
            fg=UI_COLORS["muted"],
            bg=card_surface,
            bd=0,
        )
        pick_label.pack(anchor="w", pady=(scaled(2, scale), 0))
        row["pick_label"] = pick_label

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
                if row.get("strength_bar") is not None:
                    row["strength_bar"].config(bg=badge_style["bg"])
            row["tier"] = item["tier"]

            if row.get("name_label") is not None:
                # 只显示英雄名不再拼称号：横向空间让给右侧大号胜率列。
                display_name = str(item["name"])
                if row["name_label"].cget("text") != display_name:
                    row["name_label"].config(text=display_name)

            # self=金色「已选」（我方锁定）、teammate=青色「队友」（队友锁定），
            # 其余角色（bench）不显示徽章。
            selection_role = str(item.get("selection_role") or "")
            badge_by_role = {
                "self": ("已选", UI_COLORS["selected"]),
                "teammate": ("队友", UI_COLORS["teammate"]),
            }
            if row.get("selected_badge") is not None and selection_role != row.get("selection_role"):
                role_style = badge_by_role.get(selection_role)
                if role_style is not None:
                    text, bg = role_style
                    row["selected_badge"].config(text=text, fg=UI_COLORS["header"], bg=bg)
                else:
                    row["selected_badge"].config(text="", fg=UI_COLORS["surface"], bg=UI_COLORS["surface"])
            row["selection_role"] = selection_role

            if item["win"] != row.get("win") or item["pick"] != row.get("pick"):
                win_color = UI_COLORS["green"] if item["win"] >= 0.5 else UI_COLORS["red"]
                if row.get("win_label") is not None:
                    row["win_label"].config(text=f"{item['win']:.1%}", fg=win_color)
                if row.get("pick_label") is not None:
                    row["pick_label"].config(text=f"出场 {item['pick']:.1%}")
                row["win"] = item["win"]
                row["pick"] = item["pick"]

            img_label = row.get("img_label")
            if img_label is not None and not getattr(img_label, "_hextech_avatar_loaded", False):
                # keyed 复用不再每轮重建卡片；若建卡时头像下载在途被
                # downloading_imgs 护栏跳过，这里必须补载，否则占位图会
                # 停留到卡片下一次销毁重建（审查用真实 Tk 复现过该卡死）。
                threading.Thread(
                    target=lambda champion_id=item["id"], label=img_label: self._load_and_set_img(champion_id, label),
                    daemon=True,
                ).start()
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


    def _move_overlay_to(self, x: int, y: int, height: int | None = None) -> None:
        """移动悬浮窗；height 由跟随逻辑传入客户端底缘限高值，None 表示只挪位置。"""
        try:
            current_pos = (self.root.winfo_x(), self.root.winfo_y())
            target_pos = (int(x), int(y))
            current_height = int(getattr(self, "_window_height_px", WINDOW_BASE_HEIGHT))
            target_height = current_height if height is None else int(height)
            if current_pos == target_pos and target_height == current_height:
                return
            if target_height != current_height:
                # 高度变化必须带宽度一起发完整 geometry，避免 Tk 沿用请求前的旧尺寸
                self._window_height_px = target_height
                self.root.geometry(
                    f"{self._overlay_pixel_width}x{target_height}+{target_pos[0]}+{target_pos[1]}"
                )
            else:
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

    def _sync_list_scrollbar(self) -> None:
        """滚动条只在列表内容溢出可视高度时出现；未溢出时隐藏并回到顶部。

        真机反馈：英雄数量没有溢出时常驻滑块破坏观感。
        """

        scrollbar = getattr(self, "list_scrollbar", None)
        canvas = getattr(self, "canvas", None)
        list_frame = getattr(self, "list_frame", None)
        if scrollbar is None or canvas is None or list_frame is None:
            return
        try:
            content_height = int(list_frame.winfo_reqheight())
            viewport_height = int(canvas.winfo_height())
            # 首次布局前 winfo_height 为 1，此时不做判定，等 <Configure> 再来。
            if viewport_height <= 1:
                return
            if content_height > viewport_height:
                if not scrollbar.winfo_ismapped():
                    # pack 次序决定空间分配：必须排在 expand 的 canvas 之前才拿得到宽度。
                    scrollbar.pack(side=tk.RIGHT, fill=tk.Y, before=canvas)
            else:
                if scrollbar.winfo_ismapped():
                    scrollbar.pack_forget()
                canvas.yview_moveto(0.0)
        except tk.TclError:
            logger.debug("同步列表滚动条可见性失败。", exc_info=True)

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
