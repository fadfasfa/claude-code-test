from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_runtime_alias_persistence",
    "check_latest_valid_runtime_csv_fallback",
    "check_hextech_failed_refresh_never_overwrites_csv",
    "check_hextech_success_clears_fallback_state",
    "check_precomputed_cache_freshness",
    "check_static_css_single_mount_contract",
    "check_ui_feature_flags_contract",
    "check_desktop_ui_feature_switch_contract",
)
