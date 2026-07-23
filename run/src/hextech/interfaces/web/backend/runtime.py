"""Web runtime 的兼容 facade。

`runtime_core` 保存无生命周期副作用的共享实现；LCU 轮询与 FastAPI 生命周期分别
位于独立模块。这里保留历史导入路径，但不再通过 ``import *`` 把三者缠成循环。
"""

from __future__ import annotations

from typing import Any

from hextech.interfaces.web.backend import app_lifecycle as _app_lifecycle
from hextech.interfaces.web.backend import lcu_runtime as _lcu_runtime
from hextech.interfaces.web.backend import runtime_core as _runtime_core


_RUNTIME_MODULES = (_runtime_core, _lcu_runtime, _app_lifecycle)

# 兼容 facade 允许 Desktop、API 和嵌入测试替换少量运行时注入点。直接把 core
# 函数重新导出会让函数内部仍读取 core 自己的全局变量，导致替换无效；因此这些
# 可变依赖在进入 core 前显式同步。生产路径仍只使用同一个 core 实现。
_snapshot_client = _runtime_core._snapshot_client
_static_dir = _runtime_core._static_dir
_assets_dir = _runtime_core._assets_dir
ASSET_DIR = _runtime_core.ASSET_DIR
var_path = _runtime_core.var_path
_get_resource_path = _runtime_core._get_resource_path
get_active_web_port = _runtime_core.get_active_web_port
resolve_canonical_hero_name = _runtime_core.resolve_canonical_hero_name


def _sync_runtime_core_injections() -> None:
    for name in (
        "_snapshot_client",
        "_static_dir",
        "_assets_dir",
        "ASSET_DIR",
        "var_path",
        "_get_resource_path",
        "get_active_web_port",
        "resolve_canonical_hero_name",
    ):
        setattr(_runtime_core, name, globals()[name])


def is_allowed_local_origin(origin: str | None) -> bool:
    _sync_runtime_core_injections()
    return _runtime_core.is_allowed_local_origin(origin)


def get_static_dir() -> str:
    _sync_runtime_core_injections()
    value = _runtime_core.get_static_dir()
    globals()["_static_dir"] = _runtime_core._static_dir
    return value


def get_assets_dir() -> str:
    _sync_runtime_core_injections()
    value = _runtime_core.get_assets_dir()
    globals()["_assets_dir"] = _runtime_core._assets_dir
    return value


def get_asset_search_dirs() -> tuple[str, ...]:
    _sync_runtime_core_injections()
    return _runtime_core.get_asset_search_dirs()


def find_local_asset_path(relative_path: str) -> str | None:
    _sync_runtime_core_injections()
    return _runtime_core.find_local_asset_path(relative_path)


def get_preloaded_hextech_payload(hero_name: str) -> dict[str, Any] | None:
    _sync_runtime_core_injections()
    return _runtime_core.get_preloaded_hextech_payload(hero_name)


def get_preload_hextech_status(hero_name: str) -> dict[str, Any]:
    _sync_runtime_core_injections()
    return _runtime_core.get_preload_hextech_status(hero_name)


def __getattr__(name: str) -> Any:
    """按稳定优先级转发旧 runtime 名称，避免恢复星号导入。"""

    for module in _RUNTIME_MODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({name for module in _RUNTIME_MODULES for name in dir(module)})


__all__ = sorted({name for module in _RUNTIME_MODULES for name in getattr(module, "__all__", ())})
