from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_logging_contract",
    "check_packaging_config",
    "check_bundle_manifest",
    "check_packaged_smoke_uses_explicit_feature_flags",
    "check_packaged_smoke_extracts_representative_champion_id_variants",
    "check_atomic_json_write_retries_transient_replace_conflict",
)
