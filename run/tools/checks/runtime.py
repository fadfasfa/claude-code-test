"""分域自检清单：运行时状态。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。

调用方: 见 import 此模块的代码; 关键依赖: 见 imports。
"""

from __future__ import annotations

CHECKS = (
    "check_runtime_alias_persistence",                     # 验证运行时别名持久化
    "check_latest_valid_runtime_csv_fallback",             # 验证运行时 CSV 最新有效版本回退
    "check_hextech_failed_refresh_never_overwrites_csv",   # 验证刷新失败不会覆盖已有 CSV
    "check_hextech_success_clears_fallback_state",         # 验证刷新成功后清除回退状态
    "check_precomputed_cache_freshness",                   # 验证预计算缓存时效性
    "check_precomputed_cache_returns_unpollutable_copies", # 验证预计算缓存返回不可污染副本
    "check_cached_dataframe_loader_returns_copy_and_hash_samples_middle", # 验证 DataFrame 缓存副本和中间行哈希
    "check_precomputed_atomic_write_uses_unique_temp_files", # 验证预计算缓存原子写使用唯一临时文件
    "check_static_css_single_mount_contract",              # 验证静态 CSS 单次挂载契约
    "check_ui_feature_flags_contract",                     # 验证 UI 功能开关契约
    "check_desktop_ui_feature_switch_contract",            # 验证桌面 UI 功能开关契约
)
