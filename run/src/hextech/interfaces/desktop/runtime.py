"""Desktop runtime 的稳定 facade。

窗口、服务和交互职责已拆到独立模块；保留这个历史入口仅做显式转发，避免再次把
实现细节通过星号导入复制到新的全局命名空间。
"""

from __future__ import annotations

from typing import Any

from hextech.interfaces.desktop import runtime_interaction as _runtime_interaction
from hextech.interfaces.desktop import runtime_processes as _runtime_processes
from hextech.interfaces.desktop import runtime_services as _runtime_services
from hextech.interfaces.desktop import runtime_window as _runtime_window


_RUNTIME_MODULES = (
    _runtime_window,
    _runtime_interaction,
    _runtime_services,
    _runtime_processes,
)


def __getattr__(name: str) -> Any:
    for module in _RUNTIME_MODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({name for module in _RUNTIME_MODULES for name in dir(module)})


__all__ = sorted({name for module in _RUNTIME_MODULES for name in getattr(module, "__all__", ())})
