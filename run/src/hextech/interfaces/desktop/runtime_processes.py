"""桌面窗口策略；子进程仅由App runtime owner持有。"""
from __future__ import annotations

def resolve_client_overlay_policy(
    *,
    client_visible: bool,
    game_hwnd_renderable: bool,
    gameflow_in_progress: bool,
    live_client_in_progress: bool,
    client_active: bool,
    overlay_active: bool,
    recent_client_context: bool,
    phase: str = "",
    user_hidden: bool = False,
    right_space_available: bool = True,
    manual_open: bool = False,
    display_mode: str = "champ_select_only",
) -> tuple[bool, bool]:
    """桌面显隐粗门；呈现 owner 额外校验阶段身份、时效和物理矩形。"""

    game_visible = bool(game_hwnd_renderable or gameflow_in_progress or live_client_in_progress)
    if game_visible or user_hidden or not right_space_available:
        return False, False
    if not client_visible:
        return False, False
    should_show = bool(phase == "champ_select" and client_active)
    return should_show, False
