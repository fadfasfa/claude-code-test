from __future__ import annotations

"""统一自检执行顺序。

DEFAULT_CHECKS 保持 tools.dev_checks 旧版 run_default_checks 的真实顺序；
分域模块只负责表达职责边界，避免一次性移动大量检查函数。
"""

from . import bundle, overlay, runtime, scraping, structure, synergy, web

# 全量自检清单：按 tools.dev_checks 旧版 run_default_checks 的真实顺序排列，覆盖所有分域
DEFAULT_CHECKS = (
    "check_root_entrypoints",
    "check_hextech_package_contract",
    "check_manual_alias_index",
    "check_manifest_icon_url_safety",
    "check_cdragon_force_refresh_semantics",
    "check_cdragon_source_schema_marker",
    "check_safe_detail_name_regex",
    "check_apexlol_hextech_map_size_limit",
    "check_runtime_alias_persistence",
    "check_detail_hero_param_uses_text_content",
    "check_heal_worker_contract",
    "check_latest_valid_runtime_csv_fallback",
    "check_hextech_scraper_fallback_contract",
    "check_hextech_cooldown_and_heal_fallback",
    "check_hextech_failed_refresh_never_overwrites_csv",
    "check_hextech_success_clears_fallback_state",
    "check_logging_contract",
    "check_packaging_config",
    "check_bundle_manifest",
    "check_overlay_performance_probe_contract",
    "check_game_overlay_documentation_contract",
    "check_packaged_smoke_uses_explicit_feature_flags",
    "check_packaged_smoke_extracts_representative_champion_id_variants",
    "check_atomic_json_write_retries_transient_replace_conflict",
    "check_precomputed_cache_freshness",
    "check_apex_source_snapshot_policy",
    "check_hextech_source_parser",
    "check_synergy_refresh_freshness",
    "check_synergy_snapshot_store",
    "check_synergy_structured_payloads",
    "check_detail_question_mark_augment_guard",
    "check_detail_hextech_card_layout_contract",
    "check_static_css_single_mount_contract",
    "check_web_bootstrap_avoids_load_event_gate",
    "check_api_champions_uses_stable_catalog_before_network_snapshot",
    "check_redirect_api_does_not_sync_preload_before_response",
    "check_redirect_api_defers_browser_open_before_response",
    "check_detail_api_defers_cold_local_processing",
    "check_detail_renders_before_deferred_icon_catalog",
    "check_ui_feature_flags_contract",
    "check_overlay_hint_cache_contract",
    "check_overlay_runtime_paths_contract",
    "check_overlay_event_channel_contract",
    "check_overlay_context_contract",
    "check_lol_window_contract",
    "check_official_overlay_provider_contract",
    "check_overlay_vision_sidecar_contract",
    "check_game_overlay_module_contract",
    "check_desktop_ui_feature_switch_contract",
    "check_synergy_alias_collision_guard",
    "check_synergy_api_quarantines_duplicate_pollution",
    "check_synergy_playwright_calibrator_contract",
    "check_no_legacy_imports",
)

# 仅 Overlay 相关检查子集：用于 overlay 专项验收
OVERLAY_ONLY_CHECKS = (
    "check_overlay_performance_probe_contract",
    "check_game_overlay_documentation_contract",
    "check_ui_feature_flags_contract",
    "check_overlay_hint_cache_contract",
    "check_overlay_runtime_paths_contract",
    "check_overlay_event_channel_contract",
    "check_overlay_context_contract",
    "check_lol_window_contract",
    "check_official_overlay_provider_contract",
    "check_overlay_vision_sidecar_contract",
    "check_game_overlay_module_contract",
    "check_desktop_ui_feature_switch_contract",
)

# 按分域组织的检查项映射，供 dev_checks 按需加载
DOMAIN_CHECKS = {
    "structure": structure.CHECKS,
    "web": web.CHECKS,
    "overlay": overlay.CHECKS,
    "bundle": bundle.CHECKS,
    "scraping": scraping.CHECKS,
    "synergy": synergy.CHECKS,
    "runtime": runtime.CHECKS,
}
