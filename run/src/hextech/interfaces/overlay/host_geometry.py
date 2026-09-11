"""绑定 HWND 的实时几何与 Canvas 呈现资格；不枚举或切换游戏身份。"""
from __future__ import annotations
from .host_platform import _apply_overlay_rect, _ensure_overlay_window_styles
from .host_presentation import hide_overlay_presentation, invalidate_overlay_presentation
from .host_visibility import _target_overlay_geometry
from hextech.modules.vision.window import is_window_renderable, _window_client_rect


def refresh_bound_geometry(root, config, visibility, target):
    live_rect = None
    if target is not None and is_window_renderable(int(target[0])):
        # 只有非阻塞 Win32 客户区查询；不在 Tk tick 枚举窗口、进程或请求网络。
        live_rect = _window_client_rect(int(target[0]), allow_window_fallback=False)
    visibility["display_context_refresh"] = bool(
        target and (visibility.get("target_hwnd") != target[0] or visibility.get("target_rect") != live_rect)
    )
    visibility["geometry_source"] = "live_client_rect" if live_rect is not None else "unavailable"
    visibility["geometry_observation_changed"] = bool(target and live_rect and target[1] != live_rect)
    if target is None or live_rect is None:
        if visibility.get("target_hwnd") is not None or visibility.get("target_rect") is not None:
            invalidate_overlay_presentation(visibility, reason="geometry_unavailable")
        visibility.update(target_hwnd=None, target_rect=None, pending_geometry="", applied_geometry="")
        return
    hwnd, rect = target[0], live_rect
    previous = visibility.get("target_rect")
    if visibility.get("target_hwnd") != hwnd or previous != rect:
        visibility["geometry_version"] = int(visibility.get("geometry_version") or 0) + 1
        invalidate_overlay_presentation(visibility, reason="geometry_changed")
    previous_size = (previous[2]-previous[0], previous[3]-previous[1]) if previous else None
    if visibility.get("target_hwnd") != hwnd or previous_size != (rect[2]-rect[0], rect[3]-rect[1]):
        # 背景准备尚未返回时也不能把旧尺寸 Canvas 搬到新客户区显示。
        if visibility.get("window_visible"):
            hide_overlay_presentation(root, visibility)
            visibility["window_visible"] = False
        visibility.pop("prepared_shell_key", None)
        visibility.pop("render_semantic_key", None)
    visibility["target_hwnd"] = hwnd
    visibility["target_rect"] = rect
    next_geometry = _target_overlay_geometry(rect, dict(config))
    visibility["pending_geometry"] = next_geometry
    if next_geometry != visibility.get("applied_geometry") or hwnd != visibility.get("applied_target_hwnd"):
        # 首次显示前也应用尺寸；不能先把 detecting shell 画进 Tk 的 1×1 占位区。
        root.geometry(next_geometry)
        _apply_overlay_rect(root, rect)
        _ensure_overlay_window_styles(root, config)
        root.update_idletasks()
        visibility["applied_geometry"] = next_geometry
        visibility["applied_target_hwnd"] = hwnd


def refresh_display_geometry(root, visibility, display_context):
    """在 Host 更新显示上下文后调用；DPI 变化必须撤下旧 surface 后重绘。"""
    key = (str(display_context.get("monitor_device") or ""), display_context.get("dpi_scale", 1.0))
    previous = visibility.get("presentation_display_key")
    visibility["presentation_display_key"] = key
    if previous is None or previous == key:
        return
    visibility["geometry_version"] = int(visibility.get("geometry_version") or 0) + 1
    invalidate_overlay_presentation(visibility, reason="display_context_changed")
    if previous[1] != key[1]:
        if visibility.get("window_visible"):
            hide_overlay_presentation(root, visibility)
            visibility["window_visible"] = False
        visibility.pop("prepared_shell_key", None)
        visibility.pop("render_semantic_key", None)
