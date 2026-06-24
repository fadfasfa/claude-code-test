"""应用核心轻量事件合同。

这里只定义跨桌面、Web 和 overlay 可以共享的事件类型边界；overlay 的三槽位文件协议
仍由 `hextech.overlay.events` 单独维护，避免核心层反向绑定游戏内显示细节。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias


EventName: TypeAlias = str
EventPayload: TypeAlias = dict[str, Any]
EventHandler: TypeAlias = Callable[[EventName, EventPayload], None]


__all__ = ["EventName", "EventPayload", "EventHandler"]
