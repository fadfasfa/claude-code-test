"""抓取层运行根与稳定资源路径。

本模块只计算源码态、冻结态和便携运行态路径，不导入抓取器或图标解析逻辑。
`version_sync` 与 `icon_resolver` 共同依赖这里，避免两者互相导入。
"""

from __future__ import annotations

import os
import sys


def _get_script_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_packaged_user_base_dir() -> str:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return os.path.join(local_app_data, "HextechNexus")
    app_data = os.getenv("APPDATA", "").strip()
    if app_data:
        return os.path.join(app_data, "HextechNexus")
    return os.path.join(os.path.expanduser("~"), ".hextech_nexus")


def bootstrap_runtime_environment() -> str:
    """规范运行时根目录，兼容终端、编辑器与打包程序入口。"""
    if getattr(sys, "frozen", False):
        # 冻结态不接受 HEXTECH_BASE_DIR 覆盖，避免便携包根被当作可写运行态目录。
        runtime_base = _get_packaged_user_base_dir()
    else:
        runtime_base = os.getenv("HEXTECH_BASE_DIR", "").strip()

    if runtime_base:
        runtime_base = os.path.abspath(runtime_base)
    else:
        runtime_base = _get_script_dir()

    script_dir = _get_script_dir()
    for candidate in (runtime_base, script_dir):
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)

    return runtime_base


def get_resource_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return _get_script_dir()


RUNTIME_BASE_DIR = bootstrap_runtime_environment()


def get_base_dir() -> str:
    return RUNTIME_BASE_DIR


RESOURCE_DIR = get_resource_dir()
BASE_DIR = get_base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DATA_DIR = os.path.join(DATA_DIR, "static")
INDEX_DATA_DIR = os.path.join(DATA_DIR, "indexes")
RUNTIME_DATA_DIR = os.path.join(DATA_DIR, "runtime")
RAW_DATA_DIR = (
    os.path.join(RUNTIME_DATA_DIR, "raw")
    if getattr(sys, "frozen", False)
    else os.path.join(DATA_DIR, "raw")
)
ASSET_DIR = os.path.join(BASE_DIR, "assets")
