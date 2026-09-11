"""前台客户端的有界置顶租约；只写自有wrapper，不争抢激活。"""
from collections import deque
import time

import win32gui
import pywintypes

from .client_layer import client_is_foreground, require_desktop_wrapper

LAYER_FLAGS = 0x0010 | 0x0001 | 0x0002 | 0x0200 | 0x4000  # NOACTIVATE/SIZE/MOVE/OWNERZORDER/ASYNC
PROBE_INTERVAL = .100
PROBE_LIMIT = 24
PROBE_BUDGET_SECONDS = .002


def probe_occlusion(hwnd: int, owner: int) -> dict:
    """仅向前检查有限窗口矩形，不读取标题/内容；相交仅代表潜在遮挡。"""
    rect = win32gui.GetWindowRect(hwnd)
    candidate = win32gui.GetWindow(hwnd, 3)  # GW_HWNDPREV
    started = time.perf_counter()
    count = 0
    while candidate and count < PROBE_LIMIT and time.perf_counter()-started < PROBE_BUDGET_SECONDS:
        count += 1
        try:
            if (candidate != owner and win32gui.IsWindowVisible(candidate) and not win32gui.IsIconic(candidate)
                and win32gui.GetWindow(candidate, 4) != hwnd):  # 不与自身tooltip竞争
                other = win32gui.GetWindowRect(candidate)
                if rect[0] < other[2] and rect[2] > other[0] and rect[1] < other[3] and rect[3] > other[1]:
                    return {"occluder_hwnd": int(candidate), "scanned": count, "truncated": False}
            candidate = win32gui.GetWindow(candidate, 3)
        except pywintypes.error:
            return {"occluder_hwnd": 0, "scanned": count, "truncated": True}
    return {"occluder_hwnd": 0, "scanned": count, "truncated": bool(candidate)}


class ForegroundLayerLease:
    def __init__(self):
        self._corrections = deque()
        self._total = 0
        self._probe_at = float("-inf")
        self._probe = {"occluder_hwnd": 0, "scanned": 0, "truncated": False}
        self._identity = None

    def maintain(self, hwnd: int, owner: int, *, eligible: bool, now: float | None = None) -> dict:
        started = time.perf_counter()
        timestamp = time.monotonic() if now is None else now
        require_desktop_wrapper(hwnd)
        identity = (hwnd, owner)
        if identity != self._identity:
            self._identity = identity
            self._probe_at = float("-inf")
        while self._corrections and timestamp-self._corrections[0] >= 1.0:
            self._corrections.popleft()
        native_owner = win32gui.GetWindow(hwnd, 4)
        owner_matches = native_owner == 0
        foreground_matches = client_is_foreground(owner)
        desired = bool(eligible and owner_matches and foreground_matches
                       and win32gui.IsWindowVisible(hwnd))
        actual = bool(win32gui.GetWindowLong(hwnd, -20) & 8)
        reason = "foreground_topmost" if desired else (
            "unexpected_native_owner" if not owner_matches else
            "client_not_foreground" if eligible and not foreground_matches else "lease_revoked")
        corrected = False
        if not desired:
            if actual:
                win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, LAYER_FLAGS)
            self._probe_at = float("-inf")
            self._probe = {"occluder_hwnd": 0, "scanned": 0, "truncated": False}
        else:
            if timestamp-self._probe_at >= PROBE_INTERVAL:
                self._probe = probe_occlusion(hwnd, owner)
                self._probe_at = timestamp
            if not actual or self._probe["occluder_hwnd"]:
                if len(self._corrections) >= 3:
                    reason = "layer_conflict"
                elif client_is_foreground(owner):
                    win32gui.SetWindowPos(hwnd, -1, 0, 0, 0, 0, LAYER_FLAGS)
                    self._corrections.append(timestamp)
                    self._total += 1
                    corrected = True
                    self._probe = probe_occlusion(hwnd, owner)
                    self._probe_at = timestamp
                else:
                    desired = False
                    reason = "lease_revoked"
                    if actual:
                        win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, LAYER_FLAGS)
            if desired and self._probe["truncated"] and reason != "layer_conflict":
                reason = "layer_probe_incomplete"
        actual = bool(win32gui.GetWindowLong(hwnd, -20) & 8)
        if desired and reason == "foreground_topmost":
            if not actual:
                reason = "layer_pending"
            elif self._probe["occluder_hwnd"]:
                reason = "layer_obscured"
        return {"desired_topmost": desired, "actual_topmost": actual,
                "native_owner": int(native_owner), "independent_window": owner_matches,
                "client_foreground": foreground_matches,
                "owner_matches": owner_matches, "reason": reason, "corrected": corrected,
                "occlusion": dict(self._probe), "corrections_in_window": len(self._corrections),
                "corrections_total": self._total, "last_check_ms": (time.perf_counter()-started)*1000}


class DesktopForegroundLayerMixin:
    """GUI适配留在租约模块，桌面视图只调用既有维护入口。"""

    def _maintain_foreground_layer(self, client_hwnd: int, *, eligible: bool) -> dict:
        from .client_layer import desktop_wrapper_hwnd
        lease = getattr(self, "_foreground_layer_lease", None)
        if lease is None:
            lease = self._foreground_layer_lease = ForegroundLayerLease()
        result = lease.maintain(desktop_wrapper_hwnd(self.root), client_hwnd, eligible=eligible)
        self._window_topmost = result["actual_topmost"]
        return result
