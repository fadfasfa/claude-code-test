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
from collections.abc import Mapping

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
WINDOW_EXPANDED_GEOMETRY = "320x740"
WINDOW_COLLAPSED_GEOMETRY = "80x740"
UI_COLORS = {
    "base": "#010A13",
    "header": "#050F1B",
    "surface": "#111C2E",
    "surface_alt": "#0B1626",
    "border": "#2A3B55",
    "gold": "#C89B3C",
    "cyan": "#2DD4BF",
    "green": "#32D784",
    "red": "#C45D5B",
    "text": "#F5F8FF",
    "muted": "#9EAABC",
    "dim": "#667188",
    "warn": "#F5C26B",
    "error": "#F38BA8",
}


def _format_game_overlay_host_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    return {
        "user_disabled": "已关闭",
        "gameflow_not_in_progress": "等待实际对局",
        "game_window_missing": "等待游戏窗口",
        "game_window_not_renderable": "游戏窗口不可渲染",
        "game_not_foreground": "切回游戏后显示",
        "selection_window_inactive": "等待海克斯选择",
        "visible_detecting": "检测选择中",
        "visible_partial": "部分识别",
        "visible_ready": "已显示",
    }.get(reason, "等待选择")

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
        self._threads_lock = threading.Lock()
        self.web_port_file = WEB_PORT_FILE
        self.feature_flags = load_ui_feature_flags()
        self.web_process = None
        self.runtime_supervisor = None
        self._control_instance_id = f"ui-{os.getpid()}-{int(time.time() * 1000)}"
        self._supervisor_lease_stop = threading.Event()
        self._supervisor_lease_thread: threading.Thread | None = None
        self._start_runtime_supervisor()
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
        self.current_candidate_groups = {"selected_champion_ids": [], "bench_champion_ids": []}
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
        self._overlay_watchdog_lock = threading.Lock()
        self._overlay_operation_lock = threading.Lock()
        self._game_overlay_desired_enabled = bool(self.feature_flags.get("game_overlay_enabled"))
        self._feature_toggle_busy: set[str] = set()
        self._feature_toggle_lock = threading.Lock()
        self._closing = False

        self.root = tk.Tk()
        self.root.title("Hextech 伴生系统")
        self.root.geometry(WINDOW_EXPANDED_GEOMETRY)
        self.root.configure(bg=UI_COLORS["base"])
        self.root.attributes("-alpha", 1.0, "-topmost", False)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._apply_persisted_feature_flags()
        self._init_core_engine()
        self.check_and_sync_data()
        self.start_background_scraper()
        self._start_overlay_status_polling()

    def _start_runtime_supervisor(self) -> None:
        """启动独立执行面，并用非 UI 线程续租，避免 Tk 主循环卡顿误杀运行态。"""

        try:
            self.runtime_supervisor = ui_runtime.start_runtime_supervisor_process(parent_pid=os.getpid())
            self._start_supervisor_lease_thread()
        except Exception:
            logger.exception("Runtime Supervisor 启动失败，暂保留旧 ServiceManager 兼容路径。")
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
        self.title_frame = tk.Frame(self.root, bg=UI_COLORS["header"])
        self.title_frame.pack(fill=tk.X)

        self.title_bar = tk.Label(
            self.title_frame,
            text="备战席",
            bg=UI_COLORS["header"],
            fg=UI_COLORS["text"],
            font=("Microsoft YaHei", 12, "bold"),
            pady=8,
        )
        self.title_bar.pack(side=tk.LEFT, padx=(10, 0))
        self.title_bar.bind("<ButtonPress-1>", self.start_move)
        self.title_bar.bind("<B1-Motion>", self.do_move)
        # 双击标题栏在 320 px / 80 px 之间切换，便于单屏游戏窗口模式让出主屏视线
        self.title_bar.bind("<Double-Button-1>", self._toggle_collapse)

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

        self.canvas = tk.Canvas(self.root, bg=UI_COLORS["base"], highlightthickness=0)
        self.list_frame = tk.Frame(self.canvas, bg=UI_COLORS["base"])
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=(10, 0), pady=(6, 4))
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.status_label = tk.Label(
            self.root,
            text="系统初始化中...",
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=("Microsoft YaHei", 8),
        )
        self.status_label.pack(side=tk.BOTTOM, pady=5)
        self._refresh_feature_toggle_styles()

    def _build_feature_toggle(
        self,
        text: str,
        variable: "tk.BooleanVar",
        command,
        *,
        accent: str,
    ) -> "tk.Frame":
        frame = tk.Frame(self.feature_frame, bg=UI_COLORS["base"], cursor="hand2", padx=2, pady=2)
        dot = tk.Canvas(frame, width=15, height=15, bg=UI_COLORS["base"], highlightthickness=0, bd=0, cursor="hand2")
        dot.pack(side=tk.LEFT, padx=(0, 5))
        label = tk.Label(
            frame,
            text=text,
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=("Microsoft YaHei", 9, "bold"),
            cursor="hand2",
        )
        label.pack(side=tk.LEFT)

        toggle = {"frame": frame, "dot": dot, "label": label, "variable": variable, "accent": accent}
        self._feature_toggle_widgets.append(toggle)

        def _on_click(_event=None) -> None:
            if self._feature_toggle_is_busy(text):
                self._set_status(f"{text} 正在切换中...", UI_COLORS["warn"])
                return "break"
            variable.set(not bool(variable.get()))
            self._refresh_feature_toggle_styles()
            command()
            return "break"

        for widget in (frame, dot, label):
            widget.bind("<Button-1>", _on_click)
        return frame

    def _refresh_feature_toggle_styles(self) -> None:
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
                    dot.create_oval(4, 4, 11, 11, fill=accent, outline=accent)
                    dot.create_oval(2, 2, 13, 13, outline=accent)
                    label.config(fg=UI_COLORS["warn"] if busy else UI_COLORS["text"])
                else:
                    dot.create_oval(4, 4, 11, 11, fill="", outline=UI_COLORS["dim"])
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
        toggle_name = "Web 前端"
        enabled = bool(self.web_frontend_var.get())
        self._set_feature_toggle_busy(toggle_name, True)
        self._set_status("正在切换 Web 前端...", UI_COLORS["warn"])

        def worker() -> None:
            error: Exception | None = None
            browser_opened = True
            try:
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
                if enabled:
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
                    self.web_frontend_var.set(self.service_manager.is_web_running())
                    self._try_persist_feature_flags_from_controls()
                    self._set_status(f"Web 前端切换失败: {error}", UI_COLORS["error"])
                self._set_feature_toggle_busy(toggle_name, False)

            self._run_on_ui_thread(finish)

        self._start_tracked_thread(worker, name="hextech-toggle-web")

    def _toggle_game_overlay(self) -> None:
        toggle_name = "游戏内显示"
        enabled = bool(self.game_overlay_var.get())
        self._game_overlay_desired_enabled = enabled
        self._set_feature_toggle_busy(toggle_name, True)
        self._set_status("正在启动游戏内显示..." if enabled else "正在关闭游戏内显示...", UI_COLORS["warn"])

        def worker() -> None:
            error: Exception | None = None
            try:
                with self._overlay_operation_lock:
                    if self._closing:
                        return
                    if enabled:
                        self.service_manager.start_game_overlay()
                    else:
                        self.service_manager.stop_game_overlay()
                        self._raise_if_service_error("game_overlay")
            except Exception as exc:
                error = exc
                if enabled:
                    self.service_manager.stop_game_overlay()

            def finish() -> None:
                if error is None:
                    self._persist_feature_flags_from_controls()
                    self._set_status(
                        "游戏内显示已启动，等待选择窗口" if enabled else "游戏内显示已关闭",
                        UI_COLORS["green"] if enabled else UI_COLORS["muted"],
                    )
                else:
                    self.game_overlay_var.set(self.service_manager.is_game_overlay_running())
                    self._game_overlay_desired_enabled = bool(self.game_overlay_var.get())
                    self._try_persist_feature_flags_from_controls()
                    self._set_status(f"游戏内显示切换失败: {error}", UI_COLORS["error"])
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
                cache_payload = build_overlay_hint_cache_from_precomputed(
                    include_private_stats=desired_private_stats,
                    source_tag="desktop-game-overlay",
                )
                write_overlay_hint_cache(cache_payload)
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
        logger.info("桌面启动自愈刷新已停用：refresh 由 Runtime Supervisor action 发起。")

    def _set_status(self, text, color):
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.config(text=text, fg=color)

    def _start_overlay_status_polling(self) -> None:
        self._overlay_status_after_id = self.root.after(1000, self._refresh_overlay_status_summary)

    def _kick_game_overlay_watchdog(self) -> None:
        if self._closing or self._feature_toggle_is_busy("游戏内显示"):
            return
        if not self._overlay_watchdog_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                with self._overlay_operation_lock:
                    if self._closing:
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
            # main 原本调用 overlay_service_manager.read_overlay_status_snapshot；PR 已把状态聚合
            # 下沉到 ServiceManager.get_status_snapshot，这里改读新 schema 的字段。
            overlay_enabled = bool(self.game_overlay_var.get())
            self._kick_game_overlay_watchdog()
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
                self._set_status(f"游戏内显示: {reason} / {sidecar_text}", color)
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
        logger.info("兼容旧入口：桌面不再直接调用 refresh_backend_data。")

    def load_data(self):
        return self._data_loader.get_df().copy()

    def on_hero_click(self, champ_id, hero_name):
        """处理英雄卡片点击，并触发终端输出与页面跳转。"""

        ui_runtime.handle_hero_click(self, champ_id, hero_name)

    def lcu_polling_loop(self):
        ui_runtime.lcu_polling_loop(self)

    def _load_and_set_img(self, champ_id, label):
        ui_runtime.load_and_set_img(self, champ_id, label)

    def _candidate_groups_from_input(self, hero_ids) -> dict[str, list[str]]:
        if isinstance(hero_ids, Mapping):
            selected = hero_ids.get("selected_champion_ids") or hero_ids.get("selected") or []
            bench = hero_ids.get("bench_champion_ids") or hero_ids.get("bench") or []
            return {
                "selected_champion_ids": [str(value) for value in selected if str(value or "").strip()],
                "bench_champion_ids": [str(value) for value in bench if str(value or "").strip()],
            }
        values = list(hero_ids or [])
        return {
            "selected_champion_ids": [],
            "bench_champion_ids": [str(value) for value in values if str(value or "").strip()],
        }

    def _build_candidate_display_list(self, hero_ids, current_df) -> list[dict]:
        candidate_groups = self._candidate_groups_from_input(hero_ids)
        id_col = detect_hero_id_column(current_df)
        if not id_col:
            return []

        rows_by_id: dict[str, dict] = {}
        for _, row in current_df.iterrows():
            raw_id = row.get(id_col, row.get("英雄 ID", row.get("ID", "")))
            hero_id = str(raw_id or "").strip()
            if not hero_id or hero_id in rows_by_id:
                continue
            try:
                win = float(row.get("英雄胜率", row.get("胜率", 0.5)))
            except (TypeError, ValueError):
                win = 0.5
            try:
                pick = float(row.get("英雄出场率", row.get("出场率", 0.1)))
            except (TypeError, ValueError):
                pick = 0.1
            rows_by_id[hero_id] = {
                "id": hero_id,
                "name": row.get("英雄名称", row.get("英雄名", "未知")),
                "win": win,
                "pick": pick,
                "tier": row.get("英雄评级", row.get("评级", "T?")),
            }

        display_list: list[dict] = []
        seen: set[str] = set()
        for group_name in ("selected_champion_ids", "bench_champion_ids"):
            for hero_id in candidate_groups[group_name]:
                if hero_id in seen:
                    continue
                item = rows_by_id.get(hero_id)
                if item:
                    seen.add(hero_id)
                    display_list.append(dict(item))
        return sorted(display_list, key=lambda item: item["win"], reverse=True)

    def update_ui(self, hero_ids):
        if self._ui_render_in_progress:
            self._pending_ui_refresh = hero_ids
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
                    fg=UI_COLORS["warn"],
                    bg=UI_COLORS["base"],
                    font=("Microsoft YaHei", 9),
                ).pack(pady=20)
                return

            self.status_label.config(text="实时数据已挂载", fg=UI_COLORS["green"])

            with self._df_lock:
                current_df = self.df

            display_list = self._build_candidate_display_list(hero_ids, current_df)

            for item in display_list:
                card = tk.Frame(
                    self.list_frame,
                    bg=UI_COLORS["surface"],
                    highlightthickness=1,
                    highlightbackground=UI_COLORS["border"],
                    pady=3,
                    padx=4,
                    cursor="hand2",
                )
                card.pack(fill=tk.X, pady=3, padx=(0, 10))

                ribbon_color = UI_COLORS["green"] if item["win"] >= 0.5 else UI_COLORS["red"]
                ribbon = tk.Frame(card, bg=ribbon_color, width=3)
                ribbon.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))

                img_label = tk.Label(
                    card,
                    bg=UI_COLORS["surface"],
                    highlightthickness=1,
                    highlightbackground=UI_COLORS["gold"],
                )
                img_label.pack(side=tk.LEFT, padx=(0, 8))
                threading.Thread(target=lambda i=item["id"], l=img_label: self._load_and_set_img(i, l), daemon=True).start()

                # 折叠态只渲染头像 + T 级标签，省掉胜率/出场率/胜率条
                if self._collapsed:
                    tk.Label(
                        card,
                        text=item["tier"],
                        font=("Microsoft YaHei", 9, "bold"),
                        fg=UI_COLORS["text"],
                        bg=UI_COLORS["surface"],
                    ).pack(side=tk.LEFT)

                    def bind_collapsed_click(widget, cid, name):
                        widget.bind("<Button-1>", lambda e, c=cid, n=name: self.on_hero_click(c, n))
                        for child in widget.winfo_children():
                            bind_collapsed_click(child, cid, name)

                    bind_collapsed_click(card, item["id"], item["name"])
                    continue

                info = tk.Frame(card, bg=UI_COLORS["surface"])
                info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

                title = self.core_data.get(str(item["id"]), {}).get("title", "")
                full_name = f"{item['name']} {title}".strip() if title else item["name"]

                tk.Label(
                    info,
                    text=f"[{item['tier']}] {full_name}",
                    font=("Microsoft YaHei", 9, "bold"),
                    fg=UI_COLORS["text"],
                    bg=UI_COLORS["surface"],
                ).pack(anchor="w")
                tk.Label(
                    info,
                    text=f"胜率: {item['win']:.1%} | 出场: {item['pick']:.1%}",
                    font=("Microsoft YaHei", 8),
                    fg=UI_COLORS["muted"],
                    bg=UI_COLORS["surface"],
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
        else:
            self.root.geometry(WINDOW_EXPANDED_GEOMETRY)
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
        self.update_ui(self.current_candidate_groups)

    def _manual_follow_cooldown_elapsed(self, cooldown_seconds: float) -> bool:
        if self._manual_move_timestamp <= 0:
            return True
        return (time.time() - self._manual_move_timestamp) >= cooldown_seconds

    def _restore_from_terminal(self):
        self.pause_event.clear()
        self._show_overlay(topmost=True)

    def start_background_scraper(self):
        """兼容旧入口；后台刷新由 Runtime Supervisor 唯一发起。"""

        logger.info("桌面后台刷新线程已停用：refresh 由 Runtime Supervisor 统一发起。")

    def on_close(self):
        print("\n[System] 收到退出信号，正在等待数据安全落盘...")
        self._closing = True
        self.stop_event.set()
        if self._overlay_status_after_id is not None:
            try:
                self.root.after_cancel(self._overlay_status_after_id)
                self._overlay_status_after_id = None
            except tk.TclError:
                logger.debug("取消 overlay 状态轮询失败。", exc_info=True)
        with self._threads_lock:
            threads = list(self.threads)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            if thread.is_alive():
                thread.join(timeout=2)
        # PR 已把 Web 子进程与 overlay 全部下沉到 ServiceManager；这里只做 ui_runtime 与 ServiceManager 收尾，
        # 顺带保留 main 引入的 overlay 状态轮询 after_id 取消，避免 root.destroy 后回调引发 TclError。
        ui_runtime.close_companion_browser()
        ui_runtime.shutdown_desktop_executors(wait=False)
        self.service_manager.shutdown()
        self._supervisor_lease_stop.set()
        if self._supervisor_lease_thread is not None and self._supervisor_lease_thread.is_alive():
            self._supervisor_lease_thread.join(timeout=2)
        ui_runtime.stop_runtime_supervisor_process(self.runtime_supervisor)
        self.root.destroy()


def run_desktop():
    """启动桌面伴生窗口。"""

    HextechUI().root.mainloop()


if __name__ == "__main__":
    run_desktop()
