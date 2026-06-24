from __future__ import annotations

"""分域自检清单：协同数据。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_apexlol_hextech_map_size_limit",
    "check_apex_source_snapshot_policy",
    "check_synergy_refresh_freshness",
    "check_synergy_snapshot_store",
    "check_synergy_structured_payloads",
    "check_synergy_alias_collision_guard",
    "check_synergy_playwright_calibrator_contract",
)
