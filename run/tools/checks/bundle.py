from __future__ import annotations

"""分域自检清单：打包。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_logging_contract",                                               # 验证日志契约
    "check_packaging_config",                                               # 验证打包配置完整性
    "check_bundle_manifest",                                                # 验证打包清单完整性
    "check_packaged_smoke_uses_explicit_feature_flags",                     # 验证打包冒烟测试使用显式功能开关
    "check_packaged_smoke_extracts_representative_champion_id_variants",    # 验证冒烟测试提取代表性英雄 ID 变体
    "check_atomic_json_write_retries_transient_replace_conflict",           # 验证原子 JSON 写入在替换冲突时重试
)
