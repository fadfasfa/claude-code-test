"""桌面 UI 主入口。

这个文件保留 Tk 界面结构、主状态对象和主要交互方法。
后台线程、Web 协同、LCU 查询和头像下载等运行时细节委托给 `hextech.display.desktop.runtime`，
以便在保持热路径聚合的前提下，让后续需求变更有明确落点。
"""

import ctypes
import logging
import os
import sys
import threading
import time

import tkinter as tk
from hextech.catalog.runtime_store import (
    CachedDataFrameLoader,
    build_runtime_state_path,
    detect_hero_id_column,
    get_latest_csv,
)
from hextech.core.settings import load_ui_feature_flags, save_ui_feature_flags
from hextech.overlay.hints import (
    build_overlay_hint_cache_from_precomputed,
    write_overlay_hint_cache,
)
from hextech.scraping.version_sync import (
    ASSET_DIR,
    get_advanced_session,
    load_champion_core_data,
)
from hextech.overlay.lifecycle import GameOverlayController

from . import runtime as ui_runtime

from .service_manager import ServiceManager

WEB_PORT_FILE = build_runtime_state_path("web_server_port.txt")

os.makedirs(ASSET_DIR, exist_ok=True)
logger = logging.getLogger(__name__)

try:
    from hextech.core.refresh import refresh_backend_data
except ImportError:
    print("缺少核心依赖模块，请确认文件结构完整。")
    sys.exit(1)


def _lerp_hex_color(color_a: str, color_b: str, t: float) -> str:
    """两端十六进制颜色在 RGB 空间按比例 t 插值，返回 #rrggbb 形式。"""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(color_a[1:3], 16), int(color_a[3:5], 16), int(color_a[5:7], 16)
    br, bg, bb = int(color_b[1:3], 16), int(color_b[3:5], 16), int(color_b[5:7], 16)
    rr = int(ar + (br - ar) * t)
    rg = int(ag + (bg - ag) * t)
    rb = int(ab + (bb - ab) * t)
    return f"#{rr:02x}{rg:02x}{rb:02x}"


def _render_winrate_bar(canvas: "tk.Canvas", width: int, ratio: float) -> None:
    """绘制胜率条：填充区域使用暗红→中性灰青→青绿三段渐变，并在 50% 处标记温饱基准线。"""
    canvas.delete("all")
    if width <= 0:
        return
    fill_px = max(0, int(ratio * width))
    # 三段色板：暗红 → 中性灰青（50% 锚点）→ 青绿
    color_low = "#5b3037"
    color_mid = "#5c6d75"
    color_high = "#3aa17e"
    segments = 24
    for i in range(segments):
        x0 = int(fill_px * i / segments)
        x1 = int(fill_px * (i + 1) / segments)
        if x1 <= x0:
            continue
        # 段中心点对应的归一化位置（0~1 映射回 0.40~0.60 真实胜率空间）
        center_ratio = (i + 0.5) / segments * ratio if ratio > 0 else 0
        if center_ratio < 0.5:
            seg_color = _lerp_hex_color(color_low, color_mid, center_ratio / 0.5)
        else:
            seg_color = _lerp_hex_color(color_mid, color_high, (center_ratio - 0.5) / 0.5)
        canvas.create_rectangle(x0, 0, x1, 4, fill=seg_color, outline="")
    # 50% 温饱基准线（胜率 0.50 对应归一化 ratio=0.5）
    baseline_x = int(width * 0.5)
    canvas.create_line(baseline_x, 0, baseline_x, 4, fill="#6c7086", dash=(2, 2))


class HextechUI:
    """桌面伴生主界面，负责持有 UI 状态并协调后台运行时任务。"""

    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            logger.debug("设置 DPI 感知失败。", exc_info=True)

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.threads = []
        self.web_port_file = WEB_PORT_FILE
        self.feature_flags = load_ui_feature_flags()
        self.web_process = None
        self.service_manager = ServiceManager(
            start_web_func=self._spawn_web_process,
            overlay_controller=GameOverlayController(
                prepare_data_func=self._prepare_overlay_hint_cache,
            ),
            listener_interval_seconds=3.0,
        )
        self.service_manager.set_low_frequency_listener_enabled(
            self.feature_flags.get("low_frequency_listener_enabled", True)
        )
        self.service_manager.start_low_frequency_listener()

        self.session = get_advanced_session()
        self.core_data = load_champion_core_data()
        self._data_loader = CachedDataFrameLoader(get_latest_csv)

        self.df = self.load_data()
        self.current_hero_ids = set()
        self.image_cache = {}
        self._lcu_port = None
        self._lcu_token = None

        self.last_click_time = 0
        self.img_write_lock = threading.Lock()
        self.downloading_imgs = set()
        self._df_lock = threading.Lock()
        self._window_topmost = False
        self._window_visible = False
        self._auto_follow_enabled = True
        self._manual_move_timestamp = 0.0
        self._last_client_rect = None
        self._last_overlay_target_pos = None
        # 折叠态标记：True 时悬浮窗收成 80 px 极窄列表，让出主屏视线
        self._collapsed = False
        # 首次显示是否已完成吸附定位，用于解决"必须先移动客户端窗口才会跟随"的体感问题
        self._overlay_position_initialized = False
        self._hero_preload_ready = {}
        self._hero_preload_pending = set()
        self._hero_preload_lock = threading.Lock()
        self._hero_click_gate_timeout = 1.2
        self._hero_click_gate_poll_interval = 0.05
        self._hero_click_status = ""
        self._last_live_state_version = -1
        self._last_live_state_updated_at = 0.0
        self._last_live_state_source = ""
        self._last_redirect_success_base = ""
        self._last_redirect_success_at = 0.0
        self._ui_render_in_progress = False
        self._pending_ui_refresh = None
        self._collapse_render_after_id = None
        self._overlay_status_after_id = None

        self.root = tk.Tk()
        self.root.title("Hextech 伴生系统")
        self.root.geometry("320x600")
        self.root.configure(bg="#1e1e2e")
        self.root.attributes("-alpha", 0.85, "-topmost", False)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._apply_persisted_feature_flags()
        self._init_core_engine()
        self.check_and_sync_data()
        self.start_background_scraper()
        self._start_overlay_status_polling()

    def _spawn_web_process(self):
        return ui_runtime.start_web_server_process(
            self.web_port_file,
            auto_open_browser=self.feature_flags.get("auto_open_browser", True),
        )

    def _prepare_overlay_hint_cache(self) -> None:
        cache_payload = build_overlay_hint_cache_from_precomputed(
            include_private_stats=self.feature_flags.get("private_policy_stats_enabled", False),
            source_tag="desktop-game-overlay",
        )
        write_overlay_hint_cache(cache_payload)

    def _start_web_server(self):
        """后台启动网页服务，避免阻塞界面线程。"""

        try:
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

    def _build_ui(self):
        self.title_frame = tk.Frame(self.root, bg="#11111b")
        self.title_frame.pack(fill=tk.X)

        self.title_bar = tk.Label(
            self.title_frame,
            text="备战席",
            bg="#11111b",
            fg="#cdd6f4",
            font=("Microsoft YaHei", 12, "bold"),
            pady=8,
        )
        self.title_bar.pack(side=tk.LEFT, padx=(10, 0))
        self.title_bar.bind("<ButtonPress-1>", self.start_move)
        self.title_bar.bind("<B1-Motion>", self.do_move)
        # 双击标题栏在 320 px / 80 px 之间切换，便于单屏游戏窗口模式让出主屏视线
        self.title_bar.bind("<Double-Button-1>", self._toggle_collapse)

        self.feature_frame = tk.Frame(self.root, bg="#1e1e2e", padx=10, pady=8)
        self.feature_frame.pack(fill=tk.X)
        self.web_frontend_var = tk.BooleanVar(value=self.feature_flags["web_frontend_enabled"])
        self.game_overlay_var = tk.BooleanVar(value=self.feature_flags["game_overlay_enabled"])
        self.private_stats_var = tk.BooleanVar(value=self.feature_flags["private_policy_stats_enabled"])
        self.low_frequency_listener_var = tk.BooleanVar(value=self.feature_flags["low_frequency_listener_enabled"])

        self.web_frontend_check = self._build_feature_checkbutton(
            "Web 前端",
            self.web_frontend_var,
            self._toggle_web_frontend,
        )
        self.web_frontend_check.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.game_overlay_check = self._build_feature_checkbutton(
            "游戏内显示",
            self.game_overlay_var,
            self._toggle_game_overlay,
        )
        self.game_overlay_check.grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.private_stats_check = self._build_feature_checkbutton(
            "私用统计",
            self.private_stats_var,
            self._toggle_private_policy_stats,
        )
        self.private_stats_check.grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.listener_check = self._build_feature_checkbutton(
            "低频监听",
            self.low_frequency_listener_var,
            self._toggle_low_frequency_listener,
        )
        self.listener_check.grid(row=1, column=1, sticky="w")

        self.feature_status_label = tk.Label(
            self.feature_frame,
            text="Web: 未启动 | 游戏内: 未启动",
            bg="#1e1e2e",
            fg="#a6adc8",
            font=("Microsoft YaHei", 8),
            anchor="w",
        )
        self.feature_status_label.grid(row=2, column=0, columnspan=2, sticky="we", pady=(6, 0))
        self.feature_frame.grid_columnconfigure(0, weight=1)
        self.feature_frame.grid_columnconfigure(1, weight=1)

        self.canvas = tk.Canvas(self.root, bg="#1e1e2e", highlightthickness=0)
        self.list_frame = tk.Frame(self.canvas, bg="#1e1e2e")
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.status_label = tk.Label(
            self.root,
            text="系统初始化中...",
            bg="#1e1e2e",
            fg="#a6adc8",
            font=("Microsoft YaHei", 9),
        )
        self.status_label.pack(side=tk.BOTTOM, pady=5)
        self._schedule_feature_status_refresh()

    def _build_feature_checkbutton(self, text: str, variable: "tk.BooleanVar", command) -> "tk.Checkbutton":
        return tk.Checkbutton(
            self.feature_frame,
            text=text,
            variable=variable,
            command=command,
            bg="#1e1e2e",
            fg="#cdd6f4",
            activebackground="#313244",
            activeforeground="#f5d076",
            selectcolor="#313244",
            font=("Microsoft YaHei", 9),
            anchor="w",
        )

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
        self.service_manager.set_low_frequency_listener_enabled(self.feature_flags["low_frequency_listener_enabled"])

    def _try_persist_feature_flags_from_controls(self) -> None:
        try:
            self._persist_feature_flags_from_controls()
        except Exception:
            logger.exception("持久化 UI 功能开关失败。")

    def _sync_web_process_handle(self) -> None:
        self.web_process = self.service_manager.web.process if self.service_manager.is_web_running() else None

    def _raise_if_service_error(self, service_name: str) -> None:
        service = getattr(self.service_manager, service_name)
        if service.status == "error":
            raise RuntimeError(service.last_error or f"{service_name} 状态异常")

    def _apply_persisted_feature_flags(self) -> None:
        if self.feature_flags.get("web_frontend_enabled"):
            self._toggle_web_frontend()
        if self.feature_flags.get("game_overlay_enabled"):
            self._toggle_game_overlay()

    def _toggle_web_frontend(self) -> None:
        enabled = bool(self.web_frontend_var.get())
        try:
            if enabled:
                self.service_manager.start_web()
                self._sync_web_process_handle()
                if self.feature_flags.get("auto_open_browser", True):
                    if not ui_runtime.open_companion_browser(self.web_port_file):
                        self._set_status("Web 已启动，浏览器未自动打开", "#f9e2af")
            else:
                ui_runtime.close_companion_browser()
                self.service_manager.stop_web()
                self._raise_if_service_error("web")
                self._sync_web_process_handle()
            self._persist_feature_flags_from_controls()
        except Exception as exc:
            if enabled:
                ui_runtime.close_companion_browser()
                self.service_manager.stop_web()
            self._sync_web_process_handle()
            self.web_frontend_var.set(self.service_manager.is_web_running())
            self._try_persist_feature_flags_from_controls()
            self._set_status(f"Web 前端切换失败: {exc}", "#f38ba8")
        self._refresh_feature_status()

    def _toggle_game_overlay(self) -> None:
        enabled = bool(self.game_overlay_var.get())
        try:
            if enabled:
                self.service_manager.start_game_overlay()
            else:
                self.service_manager.stop_game_overlay()
                self._raise_if_service_error("game_overlay")
            self._persist_feature_flags_from_controls()
        except Exception as exc:
            if enabled:
                self.service_manager.stop_game_overlay()
            self.game_overlay_var.set(self.service_manager.is_game_overlay_running())
            self._try_persist_feature_flags_from_controls()
            self._set_status(f"游戏内显示切换失败: {exc}", "#f38ba8")
        self._refresh_feature_status()

    def _toggle_private_policy_stats(self) -> None:
        self._persist_feature_flags_from_controls()
        self._prepare_overlay_hint_cache()
        self._set_status("私用统计仅用于本机实验，存在 Riot policy 风险", "#f9e2af")

    def _toggle_low_frequency_listener(self) -> None:
        self._persist_feature_flags_from_controls()
        self._refresh_feature_status()

    def _refresh_feature_status(self) -> None:
        if not hasattr(self, "feature_status_label") or not self.feature_status_label.winfo_exists():
            return
        snapshot = self.service_manager.get_status_snapshot()
        web_status = snapshot["web"]["status"]
        overlay_status = snapshot["game_overlay"]["status"]
        vision_status = snapshot["vision_sidecar"]["status"]
        listener = snapshot["low_frequency_listener"]
        overlay_event = snapshot.get("overlay_event", {})
        listener_text = "开" if listener.get("enabled") else "关"
        checks = int(listener.get("checks", 0) or 0)
        event_reason = overlay_event.get("reason") or overlay_event.get("error") or ("active" if overlay_event.get("visible") else "inactive")
        self.feature_status_label.config(
            text=(
                f"Web: {web_status} | 游戏内: {overlay_status}/{vision_status}/{event_reason} | "
                f"低频监听: {listener_text}/{listener.get('interval_seconds', 0):.0f}s/{checks}次"
            )
        )

    def _schedule_feature_status_refresh(self) -> None:
        self._refresh_feature_status()
        self.root.after(1000, self._schedule_feature_status_refresh)

    def check_and_sync_data(self):
        threading.Thread(target=self._silent_sync, daemon=True).start()

    def _set_status(self, text, color):
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.config(text=text, fg=color)

    def _start_overlay_status_polling(self) -> None:
        self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)

    def _refresh_overlay_status_summary(self) -> None:
        """低频回显游戏内 overlay 状态，避免 running 但不可见时没有反馈。"""

        try:
            # main 原本调用 overlay_service_manager.read_overlay_status_snapshot；PR 已把状态聚合
            # 下沉到 ServiceManager.get_status_snapshot，这里改读新 schema 的字段。
            snapshot = self.service_manager.get_status_snapshot()
            sidecar = snapshot.get("vision_sidecar") if isinstance(snapshot.get("vision_sidecar"), dict) else {}
            event = snapshot.get("overlay_event") if isinstance(snapshot.get("overlay_event"), dict) else {}
            sidecar_status = str(sidecar.get("status") or "").strip()
            overlay_enabled = bool(self.feature_flags.get("game_overlay_enabled"))
            event_active = bool(event.get("active"))
            should_report = overlay_enabled or sidecar_status == "running" or event_active
            if should_report:
                reason = str(event.get("reason") or sidecar.get("last_error") or "unknown")
                color = "#a6e3a1" if event_active else "#f9e2af"
                self._set_status(f"游戏内显示: {reason} / sidecar {sidecar_status or 'unknown'}", color)
        except Exception:
            logger.debug("读取游戏内 overlay 状态失败。", exc_info=True)
        finally:
            if not self.stop_event.is_set():
                self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)

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
        new_df = self.load_data()

        def _update_on_main():
            with self._df_lock:
                self.df = new_df
            self._set_status(status_text, status_color)

        if not self._run_on_ui_thread(_update_on_main):
            with self._df_lock:
                self.df = new_df

    def _silent_sync(self):
        ui_runtime.run_silent_sync(self, refresh_backend_data)

    def load_data(self):
        return self._data_loader.get_df().copy()

    def on_hero_click(self, champ_id, hero_name):
        """处理英雄卡片点击，并触发终端输出与页面跳转。"""

        ui_runtime.handle_hero_click(self, champ_id, hero_name)

    def lcu_polling_loop(self):
        ui_runtime.lcu_polling_loop(self)

    def _load_and_set_img(self, champ_id, label):
        ui_runtime.load_and_set_img(self, champ_id, label)

    def update_ui(self, hero_ids):
        if self._ui_render_in_progress:
            self._pending_ui_refresh = list(hero_ids)
            return

        self._ui_render_in_progress = True
        try:
            for widget in self.list_frame.winfo_children():
                widget.destroy()

            with self._df_lock:
                is_empty = self.df.empty

            if not hero_ids or is_empty:
                tk.Label(
                    self.list_frame,
                    text="当前没有可用英雄，或数据仍在同步中...",
                    fg="#f9e2af",
                    bg="#1e1e2e",
                    font=("Microsoft YaHei", 10),
                ).pack(pady=20)
                return

            self.status_label.config(text="实时数据已挂载", fg="#a6e3a1")
            display_list = []

            with self._df_lock:
                current_df = self.df

            id_col = detect_hero_id_column(current_df)
            for hid in hero_ids:
                if id_col:
                    h_data = current_df[current_df[id_col] == hid]
                    if not h_data.empty:
                        row = h_data.iloc[0]
                        id_val = row.get(id_col, row.get("英雄 ID", row.get("ID", hid)))
                        name = row.get("英雄名称", row.get("英雄名", "未知"))
                        win = float(row.get("英雄胜率", row.get("胜率", 0.5)))
                        pick = float(row.get("英雄出场率", row.get("出场率", 0.1)))
                        tier = row.get("英雄评级", row.get("评级", "T?"))
                        display_list.append({"id": id_val, "name": name, "win": win, "pick": pick, "tier": tier})

            display_list = sorted(display_list, key=lambda item: item["win"], reverse=True)

            for item in display_list:
                card = tk.Frame(self.list_frame, bg="#313244", pady=5, padx=5, cursor="hand2")
                card.pack(fill=tk.X, pady=4, padx=(0, 10))

                img_label = tk.Label(card, bg="#313244")
                img_label.pack(side=tk.LEFT, padx=(0, 10))
                threading.Thread(target=lambda i=item["id"], l=img_label: self._load_and_set_img(i, l), daemon=True).start()

                # 折叠态只渲染头像 + T 级标签，省掉胜率/出场率/胜率条
                if self._collapsed:
                    tk.Label(
                        card,
                        text=item["tier"],
                        font=("Microsoft YaHei", 9, "bold"),
                        fg="#cdd6f4",
                        bg="#313244",
                    ).pack(side=tk.LEFT)

                    def bind_collapsed_click(widget, cid, name):
                        widget.bind("<Button-1>", lambda e, c=cid, n=name: self.on_hero_click(c, n))
                        for child in widget.winfo_children():
                            bind_collapsed_click(child, cid, name)

                    bind_collapsed_click(card, item["id"], item["name"])
                    continue

                info = tk.Frame(card, bg="#313244")
                info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

                title = self.core_data.get(str(item["id"]), {}).get("title", "")
                full_name = f"{item['name']} {title}".strip() if title else item["name"]

                tk.Label(
                    info,
                    text=f"[{item['tier']}] {full_name}",
                    font=("Microsoft YaHei", 10, "bold"),
                    fg="#cdd6f4",
                    bg="#313244",
                ).pack(anchor="w")
                tk.Label(
                    info,
                    text=f"胜率: {item['win']:.1%} | 出场: {item['pick']:.1%}",
                    font=("Microsoft YaHei", 9),
                    fg="#a6adc8",
                    bg="#313244",
                ).pack(anchor="w", pady=(3, 0))

                bar_canvas = tk.Canvas(info, height=4, bg="#1e1e2e", highlightthickness=0)
                bar_canvas.pack(fill=tk.X, pady=(4, 0))
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
            self._run_on_ui_thread(lambda: self._set_status("实时数据已挂载", "#a6e3a1"))

    def _toggle_collapse(self, _event=None) -> None:
        """切换悬浮窗折叠态：展开 320 px / 折叠 80 px。"""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.root.geometry("80x600")
            # 折叠态隐藏底部状态栏，保留头像列表本体
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.pack_forget()
        else:
            self.root.geometry("320x600")
            if hasattr(self, "status_label") and self.status_label.winfo_exists():
                self.status_label.pack(side=tk.BOTTOM, pady=5)
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
        self.update_ui(list(self.current_hero_ids))

    def _manual_follow_cooldown_elapsed(self, cooldown_seconds: float) -> bool:
        if self._manual_move_timestamp <= 0:
            return True
        return (time.time() - self._manual_move_timestamp) >= cooldown_seconds

    def _restore_from_terminal(self):
        self.pause_event.clear()
        self._show_overlay(topmost=True)

    def start_background_scraper(self):
        """启动后台数据刷新循环。"""

        ui_runtime.start_background_scraper(self, refresh_backend_data)

    def on_close(self):
        print("\n[System] 收到退出信号，正在等待数据安全落盘...")
        self.stop_event.set()
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=2)
        # PR 已把 Web 子进程与 overlay 全部下沉到 ServiceManager；这里只做 ui_runtime 与 ServiceManager 收尾，
        # 顺带保留 main 引入的 overlay 状态轮询 after_id 取消，避免 root.destroy 后回调引发 TclError。
        ui_runtime.close_companion_browser()
        self.service_manager.shutdown()
        if self._overlay_status_after_id is not None:
            try:
                self.root.after_cancel(self._overlay_status_after_id)
            except tk.TclError:
                logger.debug("取消 overlay 状态轮询失败。", exc_info=True)
        self.root.destroy()


def run_desktop():
    """启动桌面伴生窗口。"""

    HextechUI().root.mainloop()


if __name__ == "__main__":
    run_desktop()
