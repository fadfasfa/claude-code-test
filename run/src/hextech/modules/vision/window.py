"""LoL 游戏窗口发现与可见性判断。

优先按 ``League of Legends.exe`` 进程枚举顶层窗口，避免客户端语言改变窗口标题后
host、Vision sidecar 和桌面伴生窗口得出不同结论。标题只作为进程信息不可读时的兜底。
本模块不读取游戏内存，也不持久化 HWND；调用方每次获得的都是当前窗口快照。

调用方: display.desktop.runtime、display.desktop.service_manager、overlay.host; 关键依赖: psutil、overlay.window_titles。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import time
from ctypes import wintypes
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import psutil

from hextech.modules.vision.game_window_mode import probe_game_window_mode
from hextech.modules.vision.window_titles import LOL_GAME_WINDOW_TITLE

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover - 非 Windows 环境只运行纯函数契约
    win32gui = None
    win32process = None


LOL_GAME_PROCESS_NAMES = frozenset({"league of legends.exe"})
LOL_GAME_WINDOW_TITLES = (LOL_GAME_WINDOW_TITLE,)
DWMWA_CLOAKED = 14
GA_ROOT = 2
VK_TAB = 0x09
VK_LBUTTON = 0x01

_DISPLAY_CACHE: dict[int, tuple[float, dict[str, object]]] = {}


def window_display_context(hwnd: int, *, force: bool = False) -> dict[str, object]:
    """只读窗口所属显示器和物理 DPI；有限缓存，不依赖 EDID/屏幕英寸。"""
    now = time.monotonic()
    cached = _DISPLAY_CACHE.get(hwnd)
    if not force and cached is not None and now - cached[0] < 1.0:
        return dict(cached[1])
    result: dict[str, object] = {"status": "unavailable", "coordinate_space": "physical_client"}
    try:
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                        ("device", wintypes.WCHAR * 32)]

        user32 = ctypes.windll.user32
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
        monitor_from_window.restype = wintypes.HANDLE
        handle = monitor_from_window(hwnd, 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        query = user32.GetMonitorInfoW
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        query.restype = wintypes.BOOL
        if handle and query(handle, ctypes.byref(info)):
            rect = info.monitor
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            dpi = int(get_dpi(hwnd))
            result.update(status="available", monitor_device=info.device,
                          monitor_rect=[rect.left, rect.top, rect.right, rect.bottom],
                          dpi_scale=(dpi / 96.0 if dpi > 0 else 1.0))
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    if len(_DISPLAY_CACHE) >= 8:
        _DISPLAY_CACHE.clear()
    _DISPLAY_CACHE[hwnd] = (now, result)
    return dict(result)


WindowProbeStatus = Literal["found", "missing", "error"]


@dataclass(frozen=True)
class WindowProbeResult:
    """区分正常无窗口与探测异常，避免 Host 把编程错误伪装成 idle。"""

    status: WindowProbeStatus
    hwnd: int | None = None
    client_rect: tuple[int, int, int, int] | None = None
    observed_at: float = 0.0
    error_type: str = ""
    process_id: int = 0
    process_started_at: float = 0.0
    game_instance_id: str = ""
    identity_quality: str = "unavailable"
    game_window_mode_status: str = "unknown"
    game_window_mode: str = "unknown"
    game_window_mode_reason: str = "game_window_mode_unknown"
    game_window_mode_source: str = "game_cfg"
    game_window_mode_observed_at: float = 0.0

    @property
    def target(self) -> tuple[int, tuple[int, int, int, int]] | None:
        if self.status != "found" or not self.hwnd or self.client_rect is None:
            return None
        return int(self.hwnd), self.client_rect


def _window_process_identity(hwnd: int) -> tuple[int, float, str, str]:
    """返回跨进程可重复计算的游戏身份；读取失败时只降级，不猜测随机 session。"""

    if win32process is None:
        return 0, 0.0, "", "unavailable"
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_id = int(pid or 0)
    except (OSError, TypeError, ValueError):
        return 0, 0.0, "", "unavailable"
    if process_id <= 0:
        return 0, 0.0, "", "unavailable"
    try:
        started_at = float(psutil.Process(process_id).create_time())
    except (psutil.Error, OSError, TypeError, ValueError):
        started_at = 0.0
    if started_at > 0:
        raw = f"lol-process:{process_id}:{started_at:.6f}"
        quality = "process"
    else:
        # PID 可读但进程创建时间受限时，HWND 使同一进程内的窗口重建明确触发重新绑定。
        raw = f"lol-window:{process_id}:{int(hwnd)}"
        quality = "window_fallback"
    return process_id, started_at, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32], quality


def game_window_identity(hwnd: int) -> dict[str, object]:
    """为 Host、Vision 与 Context Broker 提供同一份窗口身份计算。"""

    process_id, process_started_at, game_instance_id, quality = _window_process_identity(int(hwnd))
    if not game_instance_id:
        raw = f"lol-hwnd:{int(hwnd)}"
        game_instance_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        quality = "hwnd_only"
    mode_probe = probe_game_window_mode(process_id) if process_id > 0 else None
    return {
        "game_instance_id": game_instance_id,
        "window_hwnd": int(hwnd),
        "process_id": process_id,
        "process_started_at": process_started_at,
        "identity_quality": quality,
        "game_window_mode_status": mode_probe.status if mode_probe is not None else "unknown",
        "game_window_mode": mode_probe.mode if mode_probe is not None else "unknown",
        "game_window_mode_reason": (
            mode_probe.reason if mode_probe is not None else "game_window_mode_unknown"
        ),
        "game_window_mode_source": mode_probe.source if mode_probe is not None else "game_cfg",
        "game_window_mode_observed_at": (
            float(mode_probe.observed_at) if mode_probe is not None else time.time()
        ),
    }


def configure_process_dpi_awareness() -> str:
    """统一 Host/Sidecar 的物理像素坐标系，优先启用 Per-Monitor V2。"""

    if not hasattr(ctypes, "windll"):
        return "unavailable"
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return "system"
    except (AttributeError, OSError, TypeError, ValueError):
        return "unavailable"


def root_window_hwnd(hwnd: int | None) -> int:
    """规范化到顶层根 HWND；Win32 API 不可用时保留原句柄。"""

    if not hwnd:
        return 0
    value = int(hwnd)
    if not hasattr(ctypes, "windll"):
        return value
    try:
        root = int(ctypes.windll.user32.GetAncestor(value, GA_ROOT))
    except (AttributeError, OSError, ValueError):
        return value
    return root or value


def is_scoreboard_key_down() -> bool:
    """返回 Tab 当前物理按下状态；API 不可用时保守返回 False。"""

    if not hasattr(ctypes, "windll"):
        return False
    try:
        return bool(int(ctypes.windll.user32.GetAsyncKeyState(VK_TAB)) & 0x8000)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def is_left_mouse_button_down() -> bool:
    """返回鼠标左键当前物理按下状态；非 Windows 或查询失败时保守返回 False。"""

    if os.name != "nt":
        return False
    try:
        return bool(int(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON)) & 0x8000)
    except (AttributeError, OSError):
        return False


def get_cursor_screen_position() -> tuple[int, int] | None:
    """返回鼠标虚拟屏幕坐标；Win32 API 不可用时返回 None。"""

    if not hasattr(ctypes, "windll"):
        return None
    try:
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def cursor_in_client_boxes(
    client_rect: tuple[int, int, int, int],
    boxes: Sequence[tuple[int, int, int, int]],
    *,
    cursor_position: tuple[int, int] | None = None,
) -> bool:
    """判断屏幕鼠标位置是否落在 client-local 卡片框内。"""

    left, top, right, bottom = (int(value) for value in client_rect)
    cursor = cursor_position if cursor_position is not None else get_cursor_screen_position()
    if cursor is None or right <= left or bottom <= top:
        return False
    local_x = int(cursor[0]) - left
    local_y = int(cursor[1]) - top
    if not (0 <= local_x < right - left and 0 <= local_y < bottom - top):
        return False
    return any(
        int(box_left) <= local_x < int(box_right)
        and int(box_top) <= local_y < int(box_bottom)
        for box_left, box_top, box_right, box_bottom in boxes
    )


def is_window_cloaked(hwnd: int | None) -> bool:
    """返回 DWM cloak 状态；API 不可用时保守视为未 cloak。"""

    if not hwnd or not hasattr(ctypes, "windll"):
        return False
    try:
        cloaked = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            int(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return result == 0 and bool(cloaked.value)
    except (AttributeError, OSError):
        return False


def is_window_renderable(hwnd: int | None) -> bool:
    """窗口必须可见、未最小化且未被 DWM cloak，才算可用于 overlay。"""

    if win32gui is None or not hwnd:
        return False
    try:
        return bool(
            win32gui.IsWindowVisible(hwnd)
            and not win32gui.IsIconic(hwnd)
            and not is_window_cloaked(hwnd)
        )
    except Exception:
        return False


def foreground_root_hwnd() -> int:
    """返回当前前台窗口的根 HWND；Win32 API 不可用时返回 0。"""

    if not hasattr(ctypes, "windll"):
        return 0
    try:
        foreground = int(ctypes.windll.user32.GetForegroundWindow())
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return root_window_hwnd(foreground)


def is_window_foreground(hwnd: int | None, *, overlay_hwnd: int | None = None) -> bool:
    """严格判断目标窗口是否为前台；overlay 自身前台不算游戏前台。"""

    if not hwnd:
        return False
    foreground = foreground_root_hwnd()
    if not foreground:
        return False
    if overlay_hwnd and foreground == root_window_hwnd(overlay_hwnd):
        return False
    return foreground == root_window_hwnd(hwnd)


def _window_process_name(hwnd: int) -> str:
    if win32process is None:
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return str(psutil.Process(pid).name() or "").strip().casefold()
    except (psutil.Error, OSError, ValueError):
        return ""


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if win32gui is None:
        return None
    try:
        left, top, right, bottom = (int(value) for value in win32gui.GetWindowRect(hwnd))
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _window_client_rect(hwnd: int, *, allow_window_fallback: bool = True) -> tuple[int, int, int, int] | None:
    """返回客户区虚拟屏幕坐标；呈现调用禁止以窗口外框代替失败客户区。"""

    if win32gui is None:
        return None
    try:
        client_left, client_top, client_right, client_bottom = (
            int(value) for value in win32gui.GetClientRect(hwnd)
        )
        screen_left, screen_top = (
            int(value) for value in win32gui.ClientToScreen(hwnd, (client_left, client_top))
        )
        width = client_right - client_left
        height = client_bottom - client_top
        if width > 0 and height > 0:
            return (screen_left, screen_top, screen_left + width, screen_top + height)
    except Exception:
        pass
    return _window_rect(hwnd) if allow_window_fallback else None


def probe_lol_game_window(
    *,
    window_titles: Iterable[str] = LOL_GAME_WINDOW_TITLES,
    process_names: Iterable[str] = LOL_GAME_PROCESS_NAMES,
    include_nonrenderable: bool = False,
) -> WindowProbeResult:
    """探测 LoL HWND，并保留 missing/error 的差异供健康状态使用。"""

    gui = win32gui
    if gui is None:
        return WindowProbeResult(status="missing", observed_at=time.time())
    accepted_processes = {str(name).strip().casefold() for name in process_names if str(name).strip()}
    accepted_titles = {str(title).strip().casefold() for title in window_titles if str(title).strip()}
    process_match: list[tuple[int, tuple[int, int, int, int]]] = []
    title_match: list[tuple[int, tuple[int, int, int, int]]] = []

    def collect(hwnd: int, _extra: object) -> bool:
        if not is_window_renderable(hwnd):
            # 桌面伴生窗需要知道最小化游戏仍存在；默认捕获/Overlay调用仍只接受可渲染窗口。
            if not include_nonrenderable:
                return True
            try:
                hidden_title = str(gui.GetWindowText(hwnd) or "").strip().casefold()
            except Exception:
                # EnumWindows 后其他应用关闭了句柄，不应使整次游戏窗口探测失败。
                return True
            if hidden_title not in accepted_titles:
                return True
        rect = _window_client_rect(hwnd)
        if rect is None:
            return True
        candidate = (int(hwnd), rect)
        if _window_process_name(hwnd) in accepted_processes:
            process_match.append(candidate)
            return True
        try:
            title = str(gui.GetWindowText(hwnd) or "").strip().casefold()
        except Exception:
            title = ""
        if title in accepted_titles:
            title_match.append(candidate)
        return True

    try:
        gui.EnumWindows(collect, None)
    except Exception as exc:
        return WindowProbeResult(status="error", observed_at=time.time(), error_type=exc.__class__.__name__)
    target = process_match[0] if process_match else (title_match[0] if title_match else None)
    if target is None:
        return WindowProbeResult(status="missing", observed_at=time.time())
    identity = game_window_identity(target[0])
    return WindowProbeResult(
        status="found",
        hwnd=target[0],
        client_rect=target[1],
        observed_at=time.time(),
        process_id=int(identity["process_id"]),
        process_started_at=float(identity["process_started_at"]),
        game_instance_id=str(identity["game_instance_id"]),
        identity_quality=str(identity["identity_quality"]),
        game_window_mode_status=str(identity["game_window_mode_status"]),
        game_window_mode=str(identity["game_window_mode"]),
        game_window_mode_reason=str(identity["game_window_mode_reason"]),
        game_window_mode_source=str(identity["game_window_mode_source"]),
        game_window_mode_observed_at=float(identity["game_window_mode_observed_at"]),
    )


def find_lol_game_window(
    *,
    window_titles: Iterable[str] = LOL_GAME_WINDOW_TITLES,
    process_names: Iterable[str] = LOL_GAME_PROCESS_NAMES,
    include_nonrenderable: bool = False,
) -> tuple[int, tuple[int, int, int, int]] | None:
    """兼容旧调用方；新 Host/Sidecar 应使用带状态的 ``probe_lol_game_window``。"""

    return probe_lol_game_window(window_titles=window_titles, process_names=process_names,
                                include_nonrenderable=include_nonrenderable).target
