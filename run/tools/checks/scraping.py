from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_cdragon_force_refresh_semantics",
    "check_cdragon_source_schema_marker",
    "check_heal_worker_contract",
    "check_hextech_scraper_fallback_contract",
    "check_hextech_detail_timeout_tail_retry",
    "check_hextech_cooldown_and_heal_fallback",
    "check_hextech_source_parser",
)
