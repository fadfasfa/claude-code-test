from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_safe_detail_name_regex",
    "check_detail_hero_param_uses_text_content",
    "check_detail_question_mark_augment_guard",
    "check_detail_hextech_card_layout_contract",
    "check_web_bootstrap_avoids_load_event_gate",
    "check_api_champions_uses_stable_catalog_before_network_snapshot",
    "check_redirect_api_does_not_sync_preload_before_response",
    "check_redirect_api_defers_browser_open_before_response",
    "check_detail_api_defers_cold_local_processing",
    "check_detail_renders_before_deferred_icon_catalog",
    "check_synergy_api_quarantines_duplicate_pollution",
)
