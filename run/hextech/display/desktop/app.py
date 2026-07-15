"""桌面 UI 主入口。

这个文件保留 Tk 界面结构、主状态对象和主要交互方法。
后台线程、Web 协同、LCU 查询和头像下载等运行时细节委托给 `hextech.display.desktop.runtime`，
以便在保持热路径聚合的前提下，让后续需求变更有明确落点。

调用方: display.desktop.runtime、hextech_ui、tests.test_desktop_diagnostics_button; 关键依赖: data_snapshot、core.settings、overlay.events。
"""

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

import tkinter as tk
from hextech.core.settings import load_ui_feature_flags, save_ui_feature_flags

from . import runtime as ui_runtime

from .startup_timing import StartupTimingProbe, build_desktop_runtime_state_path
from .single_instance import DesktopInstanceAlreadyRunning, DesktopInstanceOwner

if TYPE_CHECKING:
    from .service_manager import ServiceManager

WEB_PORT_FILE = str(build_desktop_runtime_state_path("web_server_port.txt"))
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
        "event_stale_after_tab": "等待最新选择画面",
        "event_expired": "选择数据已过期",
        "blocking_modal_present": "等待弹窗关闭",
        "scoreboard_key_down": "记分板显示中",
        "visible_detecting": "检测选择中",
        "visible_partial": "部分识别",
        "visible_ready": "已显示",
    }.get(reason, "暂不显示")


def _format_supervisor_game_overlay_status(overlay: Mapping[str, object]) -> tuple[str, str]:
    """把 Supervisor overlay 组件状态压成游戏内显示的二级状态短句。"""

    status = str(overlay.get("status") or "").strip()
    phase = str(overlay.get("phase") or "").strip()
    cache_status = str(overlay.get("cache_status") or "").strip()
    context_status = str(overlay.get("context_status") or "").strip()
    visible_reason = str(overlay.get("visible_reason") or "").strip()
    last_error = str(overlay.get("last_error") or "").strip()
    if status == "error":
        return (f"游戏内显示异常: {last_error or phase or '未知错误'}", UI_COLORS["error"])
    if status == "stopping":
        return ("游戏内显示: 正在关闭", UI_COLORS["warn"])
    if status == "stopped":
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return ("游戏内显示: 海克斯卡识别模板预热中", UI_COLORS["warn"])
        if cache_status == "ready":
            return ("游戏内显示: 识别模板已预热", UI_COLORS["muted"])
        return ("游戏内显示: 已关闭", UI_COLORS["muted"])
    if status == "starting":
        if phase == "vision_prewarming":
            return ("游戏内显示: 窗口已就绪 / 海克斯卡识别模板预热中", UI_COLORS["warn"])
        if phase in {"prepare_data", "context_start"}:
            return ("游戏内显示: 正在准备数据", UI_COLORS["warn"])
        if phase == "sidecar_start":
            return ("游戏内显示: 正在启动识别", UI_COLORS["warn"])
        if phase == "host_start":
            return ("游戏内显示: 正在启动窗口", UI_COLORS["warn"])
        return ("游戏内显示: 正在启动", UI_COLORS["warn"])
    if status == "running":
        reason = _format_game_overlay_host_reason(visible_reason) if visible_reason else "等待选择窗口"
        if context_status == "degraded":
            return (f"游戏内显示: {reason} / 上下文降级", UI_COLORS["warn"])
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return (f"游戏内显示: {reason} / 海克斯卡识别模板预热中", UI_COLORS["warn"])
        return (f"游戏内显示: {reason} / 识别已就绪", UI_COLORS["green"])
    return ("游戏内显示: 等待 Supervisor 状态", UI_COLORS["warn"])

logger = logging.getLogger(__name__)


def _empty_dataframe():
    """延迟创建空 DataFrame，避免 desktop app import 阶段加载 pandas。"""

    import pandas as pd

    return pd.DataFrame()


def export_user_diagnostics(*args, **kwargs):
    """延迟导入诊断导出，避免首屏加载 runtime_store 数据栈。"""

    from hextech.support.user_diagnostics import export_user_diagnostics as _export_user_diagnostics

    return _export_user_diagnostics(*args, **kwargs)


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
        self.startup_timing = StartupTimingProbe()
        self.startup_timing.mark("init_start")
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
        self.data_service = None
        self.service_manager: ServiceManager | None = None
        self._service_manager_lock = threading.Lock()
        self._service_manager_shutdown_in_progress: ServiceManager | None = None
        self._service_manager_shutdown_completed: ServiceManager | None = None
        self.session = None
        self.core_data = {}
        self._snapshot_client = None
        self._snapshot_generation_id = ""
        self._snapshot_watch_started = False
        self.df = None
        self._runtime_services_ready = False
        self._post_visible_bootstrap_started = False
        self._post_visible_bootstrap_done = False
        self._control_instance_id = f"ui-{os.getpid()}-{int(time.time() * 1000)}"
        self._supervisor_lease_stop = threading.Event()
        self._supervisor_lease_thread: threading.Thread | None = None
        self.current_hero_ids = set()
        self.current_candidate_groups = {"selected_champion_ids": [], "bench_champion_ids": []}
        self.image_cache = {}
        self._lcu_port = None
        self._lcu_token = None
        self._client_context_provider = None

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
        self._overlay_status_text = ""
        self._overlay_status_color = UI_COLORS["muted"]
        self._overlay_watchdog_lock = threading.Lock()
        self._overlay_operation_lock = threading.Lock()
        self._web_operation_lock = threading.Lock()
        self._fallback_web_lock = threading.Lock()
        self._fallback_web_owned = False
        self._fallback_web_starting = False
        self._fallback_web_stopping = False
        self._fallback_web_cleanup_requested = False
        self._fallback_web_user_adopted = False
        self._fallback_web_error = ""
        self._fallback_web_generation = 0
        self._fallback_web_failed_key = ""
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
        self.startup_timing.mark("tk_shell_built")
        self.root.after_idle(self._mark_first_idle_visible)
        self.root.after(50, self._schedule_post_visible_bootstrap)

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
        loaded_df = _empty_dataframe()
        service_manager = None
        try:
            self.startup_timing.mark("background_bootstrap_start")
            from hextech.data_snapshot import DataSnapshotClient
            from hextech.scraping.version_sync import ASSET_DIR, get_advanced_session, load_champion_core_data
            from .service_manager import ServiceManager

            if self._closing:
                return
            os.makedirs(ASSET_DIR, exist_ok=True)
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

            self.session = get_advanced_session()
            self.core_data = load_champion_core_data()
            self._snapshot_client = DataSnapshotClient()
            loaded_df = self.load_data()
            self.startup_timing.mark("data_ready", rows=len(loaded_df))
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
            with self._df_lock:
                self.df = loaded_df
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

    def _prepare_overlay_hint_cache(self) -> None:
        """兼容旧调用点；共享数据只能由 DataService 发布。"""

        if self.data_service is not None:
            self.data_service.refresh()

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
            padx=8,
            pady=3,
            font=("Microsoft YaHei", 8, "bold"),
            cursor="hand2",
        )
        self.diagnostics_button.pack(side=tk.RIGHT, padx=(0, 8), pady=6)

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
        self.overlay_status_label = tk.Label(
            self.root,
            text="",
            bg=UI_COLORS["base"],
            fg=UI_COLORS["muted"],
            font=("Microsoft YaHei", 8),
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
                self._set_overlay_status_summary(f"游戏内显示: {reason} / {sidecar_text}", color)
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
        if self._snapshot_client is None:
            return _empty_dataframe()
        try:
            snapshot_view = self._snapshot_client.open_view()
        except Exception:
            return _empty_dataframe()
        self._snapshot_generation_id = str(snapshot_view.status().get("generation_id") or "")
        champions = snapshot_view.get_champions()
        if not champions:
            return _empty_dataframe()
        import pandas as pd

        return pd.DataFrame(champions)

    def _snapshot_watch_loop(self) -> None:
        """监视 DataService 原子指针，首代或新代发布后刷新桌面列表。"""

        while not self.stop_event.wait(1.0):
            if self._snapshot_client is None:
                continue
            status = self._snapshot_client.status()
            generation_id = str(status.get("generation_id") or "")
            if not generation_id or generation_id == self._snapshot_generation_id:
                continue
            new_df = self.load_data()
            if new_df.empty:
                continue
            with self._df_lock:
                self.df = new_df

            def refresh_ui() -> None:
                self._set_status("统计快照已更新", UI_COLORS["green"])
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
        if isinstance(hero_ids, Mapping):
            selected = hero_ids.get("selected_champion_ids") or hero_ids.get("selected") or []
            bench = hero_ids.get("bench_champion_ids") or hero_ids.get("bench") or []
            return {
                "selected_champion_ids": [str(value) for value in selected if str(value or "").strip()],
                "bench_champion_ids": [str(value) for value in bench if str(value or "").strip()],
                "local_champion_id": str(hero_ids.get("local_champion_id") or "").strip(),
                "teammate_champion_ids": [
                    str(value) for value in hero_ids.get("teammate_champion_ids", []) if str(value or "").strip()
                ],
                "context_phase": str(hero_ids.get("context_phase") or ""),
                "context_connection_state": str(hero_ids.get("context_connection_state") or ""),
                "context_error_code": str(hero_ids.get("context_error_code") or ""),
            }
        values = list(hero_ids or [])
        return {
            "selected_champion_ids": [],
            "bench_champion_ids": [str(value) for value in values if str(value or "").strip()],
        }

    def _build_candidate_display_list(self, hero_ids, current_df) -> list[dict]:
        from hextech.catalog.runtime_store import detect_hero_id_column

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
        local_id = candidate_groups.get("local_champion_id", "")
        teammate_ids = set(candidate_groups.get("teammate_champion_ids", []))
        for group_name in ("selected_champion_ids", "bench_champion_ids"):
            for hero_id in candidate_groups[group_name]:
                if hero_id in seen:
                    continue
                item = rows_by_id.get(hero_id)
                if item:
                    seen.add(hero_id)
                    display_item = dict(item)
                    display_item["selection_role"] = (
                        "self" if hero_id == local_id else "teammate" if hero_id in teammate_ids else "bench"
                    )
                    display_list.append(display_item)
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
                current_df = self.df
                is_empty = current_df is None or current_df.empty

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

            display_list = self._build_candidate_display_list(hero_ids, current_df)
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
                role_color = "#22D3EE" if role == "self" else "#F59E0B" if role == "teammate" else UI_COLORS["border"]
                card = tk.Frame(
                    self.list_frame,
                    bg=UI_COLORS["surface"],
                    highlightthickness=1,
                    highlightbackground=role_color,
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
                        bg=UI_COLORS["surface"],
                    ).pack(side=tk.LEFT)
                    if role in {"self", "teammate"}:
                        tk.Label(
                            card,
                            text="我" if role == "self" else "队友",
                            font=("Microsoft YaHei", 7, "bold"),
                            fg="#062A30" if role == "self" else "#2D1B00",
                            bg=role_color,
                            padx=3,
                        ).pack(side=tk.RIGHT)

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

                title_row = tk.Frame(info, bg=UI_COLORS["surface"])
                title_row.pack(fill=tk.X)
                tk.Label(
                    title_row,
                    text=f"[{item['tier']}] {full_name}",
                    font=("Microsoft YaHei", 9, "bold"),
                    fg=UI_COLORS["text"],
                    bg=UI_COLORS["surface"],
                ).pack(side=tk.LEFT, anchor="w")
                if role in {"self", "teammate"}:
                    tk.Label(
                        title_row,
                        text="我的英雄" if role == "self" else "队友已选",
                        font=("Microsoft YaHei", 8, "bold"),
                        fg="#062A30" if role == "self" else "#2D1B00",
                        bg=role_color,
                        padx=5,
                        pady=0,
                    ).pack(side=tk.RIGHT)
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
        service_manager = self._take_service_manager_for_shutdown()
        if service_manager is not None:
            try:
                service_manager.shutdown()
            finally:
                with self._service_manager_lock:
                    if self._service_manager_shutdown_in_progress is service_manager:
                        self._service_manager_shutdown_in_progress = None
                        self._service_manager_shutdown_completed = service_manager
        self._supervisor_lease_stop.set()
        if self._supervisor_lease_thread is not None and self._supervisor_lease_thread.is_alive():
            self._supervisor_lease_thread.join(timeout=2)
        ui_runtime.stop_runtime_supervisor_process(self.runtime_supervisor)
        self.data_service = None
        self.root.destroy()


def run_desktop():
    """启动桌面伴生窗口。"""

    try:
        with DesktopInstanceOwner():
            HextechUI().root.mainloop()
    except DesktopInstanceAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    run_desktop()
