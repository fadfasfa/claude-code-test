from __future__ import annotations

"""分域自检清单：项目结构。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。
"""

CHECKS = (
    "check_root_entrypoints",               # 验证根目录入口点（build.py/web_server.py/hextech_ui.py）存在
    "check_python_runtime_guard_contract",  # 验证源码态入口统一切换到 Python 3.11
    "check_hextech_package_contract",       # 验证 hextech 包结构契约（__init__.py 完整性）
    "check_resource_classification_manifest",  # 验证 data 分类清单
    "check_version_data_catalog_consolidation",  # 验证版本数据收口到 data/static/version 与旧文件名投影
    "check_stable_data_compat_routes_are_whitelisted",  # 验证旧数据 URL 不泛暴露稳定版本数据目录
    "check_champion_core_projection_replaces_legacy_file",  # 验证后台旧 core 读取点改用英雄目录投影
    "check_clean_mayhem_combos_uses_core_projection",  # 验证 Mayhem 清洗不依赖旧 core 实体文件
    "check_manual_alias_index",             # 验证手动维护的别名索引与数据一致性
    "check_manifest_icon_url_safety",       # 验证图标清单中 URL 安全性（无外部恶意链接）
    "check_icon_resolver_defaults_to_resource_image_dir",  # 验证图标默认读取 data/static/assets
    "check_no_legacy_imports",              # 验证无残留的旧模块导入
)
