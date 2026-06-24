from __future__ import annotations

"""分域自检清单：协同数据。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_apexlol_hextech_map_size_limit",           # 验证 ApexLoL Hextech 映射大小限制
    "check_apex_source_snapshot_policy",              # 验证 Apex 数据源快照策略一致性
    "check_synergy_refresh_freshness",                # 验证协同数据刷新时效性
    "check_synergy_snapshot_store",                   # 验证协同快照存储完整性
    "check_synergy_structured_payloads",              # 验证协同结构化载荷格式
    "check_synergy_alias_collision_guard",            # 验证协同别名碰撞防护
    "check_synergy_playwright_calibrator_contract",   # 验证 Playwright 校准器契约
)
