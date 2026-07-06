"""分域自检清单：Overlay 视觉覆盖层。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。

调用方: 见 import 此模块的代码; 关键依赖: 见 imports。
"""

from __future__ import annotations

CHECKS = (
    "check_overlay_performance_probe_contract",        # 验证 Overlay 性能探针契约
    "check_game_overlay_documentation_contract",        # 验证游戏覆盖层文档契约
    "check_overlay_hint_cache_contract",                # 验证 Overlay 提示缓存契约
    "check_overlay_runtime_paths_contract",             # 验证 Overlay 运行时路径契约
    "check_overlay_event_channel_contract",             # 验证 Overlay 事件通道契约
    "check_overlay_context_contract",                   # 验证 Overlay 上下文契约
    "check_lol_window_contract",                        # 验证 LoL 游戏窗口检测契约
    "check_official_overlay_provider_contract",         # 验证官方 Overlay 数据源契约
    "check_overlay_vision_sidecar_contract",            # 验证 Vision Sidecar 识别契约
    "check_overlay_refresh_tool_contract",              # 验证 Overlay 视觉资源刷新工具契约
    "check_game_overlay_module_contract",               # 验证游戏覆盖层模块契约
)
