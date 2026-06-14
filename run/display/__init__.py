"""Hextech 展示层聚合出口。

这里汇总桌面 UI 与 Web 服务的稳定入口，方便根目录薄壳与其他模块复用。
实际对象按需懒加载，避免只开 overlay 或只导入 display 包时提前加载 Web/FastAPI。
"""

__all__ = ["HextechUI", "app", "run_desktop", "run_web", "run_web_server"]


def __getattr__(name: str):
    if name in {"HextechUI", "run_desktop"}:
        from .hextech_ui import HextechUI, run_desktop

        return {"HextechUI": HextechUI, "run_desktop": run_desktop}[name]
    if name in {"app", "run_web", "run_web_server"}:
        from .web_server import app, run_web, run_web_server

        return {"app": app, "run_web": run_web, "run_web_server": run_web_server}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
