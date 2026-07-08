"""分域自检清单：抓取。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。

调用方: 见 import 此模块的代码; 关键依赖: 见 imports。
"""

from __future__ import annotations

CHECKS = (
    "check_cdragon_force_refresh_semantics",
    "check_cdragon_source_schema_marker",
    "check_heal_worker_contract",
    "check_hextech_scraper_fallback_contract",
    "check_hextech_remote_failure_cooldown_and_escalation",
    "check_hextech_champion_detail_json_extracts_full_rows",
    "check_hextech_detail_fetch_prefers_cdn_json_without_browser",
    "check_hextech_detail_timeout_tail_retry",
    "check_scrapling_tls_error_contract",
    "check_scrapling_fetch_text_keeps_internal_retry",
    "check_scrapling_fetch_page_get_timeout_uses_seconds",
    "check_verify_data_source_integrity_offline_fixture_mode",
    "check_version_sync_startup_resource_guard",
    "check_hextech_cooldown_and_heal_fallback",
    "check_hextech_source_parser",
)
