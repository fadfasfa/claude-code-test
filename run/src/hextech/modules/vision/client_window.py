"""LeagueClientUx 主窗口的只读、失败闭合选择器。

同一 ``LeagueClientUx.exe`` 会创建多个同标题的隐藏辅助窗口，因此不能使用
``FindWindow`` 的首个结果。这里统一验证进程身份和可呈现的顶层 ``RCLIENT``，
并把前台优先、上一窗口保留和多候选歧义作为显式结果交给桌面调用方。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import psutil

from hextech.modules.vision.window import is_window_cloaked

try:
    import win32gui
    import win32process
except ImportError:  # pragma: no cover - 非 Windows 环境只运行纯函数契约
    win32gui = None
    win32process = None


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2

LOL_CLIENT_PROCESS_NAMES = frozenset({"leagueclientux.exe"})
LOL_CLIENT_WINDOW_CLASSES = frozenset({"rclient"})
MIN_CLIENT_WIDTH = 640
MIN_CLIENT_HEIGHT = 360
PROCESS_ID_CACHE_SECONDS = 1.0
_PROCESS_ID_CACHE: tuple[float, frozenset[int], bool] = (0.0, frozenset(), False)

ClientWindowProbeStatus = Literal["found", "missing", "ambiguous", "error"]


@dataclass(frozen=True)
class ClientWindowProbeResult:
    """桌面端可共享的客户端窗口快照。"""

    status: ClientWindowProbeStatus
    hwnd: int = 0
    client_rect: tuple[int, int, int, int] | None = None
    process_id: int = 0
    is_foreground: bool = False
    observed_at: float = 0.0
    reason: str = ""
    candidate_count: int = 0
    error_type: str = ""


@dataclass(frozen=True)
class _ClientWindowCandidate:
    hwnd: int
    process_id: int
    client_rect: tuple[int, int, int, int]


def _accepted_process_ids() -> tuple[set[int], bool]:
    """只读进程名；不要求打开高权限进程或读取可执行路径。"""

    global _PROCESS_ID_CACHE
    now = time.monotonic()
    cached_at, cached_ids, cached_complete = _PROCESS_ID_CACHE
    if now - cached_at < PROCESS_ID_CACHE_SECONDS:
        return set(cached_ids), cached_complete
    result: set[int] = set()
    try:
        for process in psutil.process_iter(["pid", "name"]):
            info = process.info
            name = str(info.get("name") or "").strip().casefold()
            pid = int(info.get("pid") or 0)
            if pid > 0 and name in LOL_CLIENT_PROCESS_NAMES:
                result.add(pid)
    except (psutil.Error, OSError, TypeError, ValueError):
        # 单次枚举失败仍允许按窗口 PID 做有限的进程名查询。
        return result, False
    _PROCESS_ID_CACHE = now, frozenset(result), True
    return result, True


def _window_process_id(hwnd: int) -> int:
    if win32process is None:
        return 0
    try:
        _thread_id, process_id = win32process.GetWindowThreadProcessId(int(hwnd))
        return int(process_id or 0)
    except (OSError, TypeError, ValueError):
        return 0


def _process_is_client(
    process_id: int,
    accepted_process_ids: set[int],
    *,
    identity_scan_complete: bool,
) -> bool:
    if process_id <= 0:
        return False
    if process_id in accepted_process_ids:
        return True
    if identity_scan_complete:
        return False
    try:
        return str(psutil.Process(process_id).name() or "").strip().casefold() in LOL_CLIENT_PROCESS_NAMES
    except (AttributeError, psutil.Error, OSError, TypeError, ValueError):
        return False


def _root_window(hwnd: int) -> int:
    gui = win32gui
    if gui is None:
        return 0
    try:
        root = int(gui.GetAncestor(int(hwnd), GA_ROOT) or 0)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return root


def _client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    gui = win32gui
    if gui is None:
        return None
    try:
        left, top, right, bottom = (int(value) for value in gui.GetClientRect(int(hwnd)))
        width = right - left
        height = bottom - top
        if width < MIN_CLIENT_WIDTH or height < MIN_CLIENT_HEIGHT:
            return None
        screen_left, screen_top = (
            int(value) for value in gui.ClientToScreen(int(hwnd), (left, top))
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return screen_left, screen_top, screen_left + width, screen_top + height


def _candidate(
    hwnd: int,
    *,
    accepted_process_ids: set[int],
    identity_scan_complete: bool,
) -> _ClientWindowCandidate | None:
    gui = win32gui
    if gui is None or not hwnd:
        return None
    value = int(hwnd)
    try:
        if not gui.IsWindow(value):
            return None
        if not gui.IsWindowVisible(value) or gui.IsIconic(value) or is_window_cloaked(value):
            return None
        if _root_window(value) != value:
            return None
        style = int(gui.GetWindowLong(value, GWL_STYLE) or 0)
        exstyle = int(gui.GetWindowLong(value, GWL_EXSTYLE) or 0)
        if style & WS_CHILD or exstyle & WS_EX_TOOLWINDOW:
            return None
        if str(gui.GetClassName(value) or "").strip().casefold() not in LOL_CLIENT_WINDOW_CLASSES:
            return None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    process_id = _window_process_id(value)
    if not _process_is_client(
        process_id,
        accepted_process_ids,
        identity_scan_complete=identity_scan_complete,
    ):
        return None
    rect = _client_rect(value)
    if rect is None:
        return None
    return _ClientWindowCandidate(hwnd=value, process_id=process_id, client_rect=rect)


def resolve_lol_client_window(*, previous_hwnd: int = 0) -> ClientWindowProbeResult:
    """选择唯一可信的 LeagueClientUx 主窗口。

    当前前台的可信主窗口优先。客户端在后台时仅保留仍有效的上一主窗口，
    供调用方观察位置和恢复；该结果的 ``is_foreground`` 为 ``False``，不能据此
    授权显示。没有上一窗口时，只接受唯一候选，多候选明确失败闭合。
    """

    observed_at = time.time()
    gui = win32gui
    if gui is None or win32process is None:
        return ClientWindowProbeResult(
            status="missing",
            observed_at=observed_at,
            reason="win32_unavailable",
        )
    accepted_process_ids, identity_scan_complete = _accepted_process_ids()
    try:
        foreground_raw = int(gui.GetForegroundWindow() or 0)
    except (AttributeError, OSError, TypeError, ValueError):
        foreground_raw = 0
    foreground_hwnd = _root_window(foreground_raw) if foreground_raw else 0
    foreground = _candidate(
        foreground_hwnd,
        accepted_process_ids=accepted_process_ids,
        identity_scan_complete=identity_scan_complete,
    )
    if foreground is not None:
        return ClientWindowProbeResult(
            status="found",
            hwnd=foreground.hwnd,
            client_rect=foreground.client_rect,
            process_id=foreground.process_id,
            is_foreground=True,
            observed_at=observed_at,
            reason="foreground_client_window",
            candidate_count=1,
        )

    previous = _candidate(
        int(previous_hwnd or 0),
        accepted_process_ids=accepted_process_ids,
        identity_scan_complete=identity_scan_complete,
    )
    if previous is not None:
        return ClientWindowProbeResult(
            status="found",
            hwnd=previous.hwnd,
            client_rect=previous.client_rect,
            process_id=previous.process_id,
            is_foreground=False,
            observed_at=observed_at,
            reason="retained_previous_client_window",
            candidate_count=1,
        )

    candidates: list[_ClientWindowCandidate] = []

    def collect(hwnd: int, _extra: object) -> bool:
        candidate = _candidate(
            int(hwnd),
            accepted_process_ids=accepted_process_ids,
            identity_scan_complete=identity_scan_complete,
        )
        if candidate is not None:
            candidates.append(candidate)
        return True

    try:
        gui.EnumWindows(collect, None)
    except Exception as exc:
        return ClientWindowProbeResult(
            status="error",
            observed_at=observed_at,
            reason="client_window_enumeration_failed",
            error_type=exc.__class__.__name__,
        )
    if len(candidates) == 1:
        selected = candidates[0]
        return ClientWindowProbeResult(
            status="found",
            hwnd=selected.hwnd,
            client_rect=selected.client_rect,
            process_id=selected.process_id,
            is_foreground=False,
            observed_at=observed_at,
            reason="unique_client_window",
            candidate_count=1,
        )
    if len(candidates) > 1:
        return ClientWindowProbeResult(
            status="ambiguous",
            observed_at=observed_at,
            reason="multiple_client_windows",
            candidate_count=len(candidates),
        )
    return ClientWindowProbeResult(
        status="missing",
        observed_at=observed_at,
        reason="client_window_missing",
    )


__all__ = ["ClientWindowProbeResult", "resolve_lol_client_window"]
