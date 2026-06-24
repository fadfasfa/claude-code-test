from __future__ import annotations

"""分域自检清单：抓取。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_cdragon_force_refresh_semantics",         # 验证 CDragon 强制刷新的语义正确性
    "check_cdragon_source_schema_marker",            # 验证 CDragon 数据源 schema 标记一致性
    "check_heal_worker_contract",                    # 验证 HealWorker 容错契约
    "check_hextech_scraper_fallback_contract",       # 验证 Hextech 抓取器的回退契约
    "check_hextech_cooldown_and_heal_fallback",      # 验证抓取冷却与 heal 回退机制
    "check_hextech_source_parser",                   # 验证 Hextech 数据源解析器完整性
)
