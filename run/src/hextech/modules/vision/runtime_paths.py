"""游戏内 overlay 运行态路径。

本模块只负责 overlay 本地 state/debug 这类运行态路径的轻量解析。它不导入
`runtime_store`，避免 host/sidecar 启动早期因为抓取或 pandas 依赖拖慢路径解析。

调用方: display.desktop.service_manager、overlay.context、overlay.events; 关键依赖: 见 imports。
"""

from __future__ import annotations

from pathlib import Path

from hextech.modules.data.ports.paths import get_var_dir


def overlay_runtime_root_dir() -> Path:
    """返回 overlay 专用运行态目录；打包与源码模式必须保持同一套规则。"""

    return get_var_dir()


def overlay_runtime_state_path(filename: str) -> str:
    """返回 overlay state 文件路径，并拒绝任何逃逸 state 根目录的文件名。"""

    root = (overlay_runtime_root_dir() / "state").resolve()
    candidate = (root / filename).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"overlay runtime state path escaped state dir: {filename}")
    return str(candidate)
