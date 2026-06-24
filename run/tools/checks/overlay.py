from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_overlay_performance_probe_contract",
    "check_game_overlay_documentation_contract",
    "check_overlay_hint_cache_contract",
    "check_overlay_runtime_paths_contract",
    "check_overlay_event_channel_contract",
    "check_overlay_context_contract",
    "check_lol_window_contract",
    "check_official_overlay_provider_contract",
    "check_overlay_vision_sidecar_contract",
    "check_game_overlay_module_contract",
)
