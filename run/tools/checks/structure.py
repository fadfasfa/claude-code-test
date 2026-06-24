from __future__ import annotations

"""分域自检清单。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_root_entrypoints",
    "check_hextech_package_contract",
    "check_manual_alias_index",
    "check_manifest_icon_url_safety",
    "check_no_legacy_imports",
)
