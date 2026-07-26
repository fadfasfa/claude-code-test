"""Desktop UI 组合类与进程入口。"""
from __future__ import annotations

from hextech.interfaces.desktop.app_shared import (  # noqa: F401 - 保留历史 app 模块导入面
    DesktopInstanceAlreadyRunning,
    DesktopInstanceOwner,
    StartupTimingProbe,
    UI_COLORS,
    WEB_PORT_FILE,
    WINDOW_BASE_HEIGHT,
    WINDOW_EXPANDED_WIDTH,
    _format_game_overlay_host_reason,
    _format_supervisor_game_overlay_status,
    ctypes,
    load_ui_feature_flags,
    logger,
    os,
    save_ui_feature_flags,
    scaled,
    sys,
    threading,
    time,
    tk,
    window_dpi_scale,
)
from hextech.interfaces.desktop.app_bootstrap import DesktopBootstrapMixin
from hextech.interfaces.desktop.background_runtime import DesktopBackgroundRuntimeMixin
from hextech.interfaces.desktop.app_controls import DesktopControlsMixin
from hextech.interfaces.desktop.app_view import DesktopViewMixin
from hextech.interfaces.desktop.service_manager import ServiceManager
# 保留 app.ui_runtime 兼容注入点，集成测试和嵌入方可替换进程启动函数。
from hextech.interfaces.desktop import runtime as ui_runtime  # noqa: F401


class HextechUI(DesktopBackgroundRuntimeMixin, DesktopBootstrapMixin, DesktopControlsMixin, DesktopViewMixin):
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
        self.champions: list[dict] = []
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
        self._champions_lock = threading.Lock()
        self._window_topmost = False
        self._window_visible = False
        self._auto_follow_enabled = True
        self._manual_move_timestamp = 0.0
        self._last_client_rect = None
        self._last_overlay_target_pos = None
        # 折叠态标记：True 时悬浮窗收成 112 px 极窄列表，让出主屏视线
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
        self._shutdown_done_event = threading.Event()
        self._initialize_background_runtime()

        self.root = tk.Tk()
        self.root.title("Hextech 伴生系统")
        # 按用户要求维持基线"狭长"观感：固定像素密度（缩放锁 1.0，不乘 DPI，
        # window_dpi_scale 保留在 app_shared 供将来选择性启用），高度基值 740；
        # 跟随客户端时由 runtime_window 按客户端底缘动态压缩高度。
        self._ui_scale = 1.0
        self._window_height_px = WINDOW_BASE_HEIGHT
        self._overlay_pixel_width = WINDOW_EXPANDED_WIDTH
        self.root.geometry(f"{self._overlay_pixel_width}x{self._window_height_px}")
        self.root.configure(bg=UI_COLORS["base"])
        self.root.attributes("-alpha", 1.0, "-topmost", False)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._build_ui()
        self._start_desktop_tray()
        self.startup_timing.mark("tk_shell_built")
        self.root.after_idle(self._mark_first_idle_visible)
        self.root.after(50, self._schedule_post_visible_bootstrap)

def run_desktop():
    """启动桌面伴生窗口。"""

    try:
        with DesktopInstanceOwner() as instance_owner:
            ui = HextechUI()
            ui.attach_instance_owner(instance_owner)
            try:
                ui.root.mainloop()
            except KeyboardInterrupt:
                # Windows 控制台退出属于正常关闭请求，不能把 Tk mainloop 误报成崩溃。
                ui.exit_application()
            ui.wait_for_shutdown(timeout_seconds=8.0)
    except DesktopInstanceAlreadyRunning as exc:
        # 第二次点击快捷方式只唤醒已存在的托盘实例；GUI 模式不能依赖 stderr。
        raise SystemExit(0 if exc.activation_sent else 2) from exc


if __name__ == "__main__":
    run_desktop()
