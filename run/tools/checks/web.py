"""分域自检清单：Web 前端。

这里只保存检查函数名，不导入 tools.dev_checks，避免脚本模式下重复加载模块状态。

调用方: 见 import 此模块的代码; 关键依赖: 见 imports。
"""

from __future__ import annotations

CHECKS = (
    "check_safe_detail_name_regex",                                          # 验证详情页名称正则安全性
    "check_detail_hero_param_uses_text_content",                             # 验证英雄详情页参数使用 textContent 而非 innerHTML
    "check_detail_question_mark_augment_guard",                              # 验证问号增强物防护
    "check_detail_hextech_card_layout_contract",                             # 验证 Hextech 卡片布局契约
    "check_web_bootstrap_avoids_load_event_gate",                            # 验证 Web 启动不依赖 load 事件阻塞
    "check_api_champions_uses_stable_catalog_before_network_snapshot",       # 验证 API 先查稳定目录再发网络请求
    "check_redirect_api_does_not_sync_preload_before_response",              # 验证重定向 API 不在响应前同步预加载
    "check_redirect_api_defers_browser_open_before_response",                # 验证重定向 API 不在响应前打开浏览器
    "check_redirect_api_handles_invalid_champion_input",                     # 验证重定向 API 非法输入不冒泡 500
    "check_detail_api_defers_cold_local_processing",                         # 验证详情 API 延迟冷本地处理
    "check_detail_renders_before_deferred_icon_catalog",                     # 验证详情渲染在延迟图标目录之前完成
    "check_synergy_api_quarantines_duplicate_pollution",                     # 验证协同 API 隔离重复污染
)
