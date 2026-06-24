"""窗口标题常量集中定义。

职责：集中维护 League of Legends 客户端与游戏内窗口的标题字符串，
供 service_manager / ui_runtime / overlay host 等模块统一引用，避免标题散落多处
硬编码。本模块只导出字符串常量，不引入任何重依赖。
"""

from __future__ import annotations


LOL_CLIENT_WINDOW_TITLE = "League of Legends"
LOL_GAME_WINDOW_TITLE = "League of Legends (TM) Client"
