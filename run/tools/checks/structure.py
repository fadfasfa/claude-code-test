from __future__ import annotations

"""分域自检清单：项目结构。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_root_entrypoints",               # 验证根目录入口点（build.py/web_server.py/hextech_ui.py）存在
    "check_hextech_package_contract",       # 验证 hextech 包结构契约（__init__.py 完整性）
    "check_manual_alias_index",             # 验证手动维护的别名索引与数据一致性
    "check_manifest_icon_url_safety",       # 验证图标清单中 URL 安全性（无外部恶意链接）
    "check_no_legacy_imports",              # 验证无残留的旧模块导入
)
