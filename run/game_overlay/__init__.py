"""独立游戏内显示模块。

本包负责游戏内 overlay 的生命周期、共享数据适配、窗口宿主和绘制。
导入包本身不得启动 Tk、Vision sidecar、Web 服务或任何后台线程。
"""

from __future__ import annotations

__all__ = ["GameOverlayController", "OverlayDataSource", "SharedOverlayDataSource"]


def __getattr__(name: str):
    if name == "GameOverlayController":
        from .lifecycle import GameOverlayController

        return GameOverlayController
    if name in {"OverlayDataSource", "SharedOverlayDataSource"}:
        from .data_source import OverlayDataSource, SharedOverlayDataSource

        return {"OverlayDataSource": OverlayDataSource, "SharedOverlayDataSource": SharedOverlayDataSource}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
