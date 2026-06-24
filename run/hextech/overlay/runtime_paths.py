"""游戏内 overlay 运行态路径。

本模块只负责 overlay 本地 state/debug 这类运行态路径的轻量解析。它不导入
`runtime_store`，避免 host/sidecar 启动早期因为抓取或 pandas 依赖拖慢路径解析。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def overlay_runtime_root_dir() -> Path:
    """返回 overlay 专用运行态目录；打包与源码模式必须保持同一套规则。"""

    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "HextechNexus" / "data" / "runtime"
        app_data = os.getenv("APPDATA", "").strip()
        if app_data:
            return Path(app_data) / "HextechNexus" / "data" / "runtime"
        return Path.home() / ".hextech_nexus" / "data" / "runtime"
    # 源码态文件位于 run/hextech/overlay/；parents[2] 才是 run/ 根。
    base_dir = Path(os.getenv("HEXTECH_BASE_DIR", "") or Path(__file__).resolve().parents[2])
    return base_dir / "data" / "runtime"


def overlay_runtime_state_path(filename: str) -> str:
    """返回 overlay state 文件路径，并拒绝任何逃逸 state 根目录的文件名。"""

    root = (overlay_runtime_root_dir() / "state").resolve()
    candidate = (root / filename).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"overlay runtime state path escaped state dir: {filename}")
    return str(candidate)
