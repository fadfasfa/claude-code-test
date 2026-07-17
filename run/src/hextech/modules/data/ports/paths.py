"""Hextech 可提交资源和本机运行态的唯一路径事实源。

源码态只读资源位于 ``run/resources``，可写运行态位于 ``run/var``；冻结态
资源来自 PyInstaller bundle，可写运行态固定为用户目录 ``HextechNexus/var``。
本模块不提供旧 ``data`` 目录回退，路径切换失败必须显式暴露。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _source_project_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _packaged_user_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus"
    app_data = os.getenv("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus"
    return Path.home() / ".hextech_nexus"


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return _source_project_root()


def _var_root() -> Path:
    if getattr(sys, "frozen", False):
        return _packaged_user_root() / "var"
    override = os.getenv("HEXTECH_VAR_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _source_project_root() / "var"


PROJECT_ROOT = _source_project_root()
BUNDLE_ROOT_DIR = os.fspath(_bundle_root())
BASE_DIR = os.fspath(_bundle_root())
RESOURCE_DIR = os.fspath(_bundle_root() / "resources")
STATIC_DATA_DIR = os.path.join(RESOURCE_DIR, "catalog")
INDEX_DATA_DIR = STATIC_DATA_DIR
ASSET_DIR = os.path.join(RESOURCE_DIR, "assets")
CHAMPION_ASSET_DIR = os.path.join(ASSET_DIR, "champions")
STARTUP_SEED_DIR = os.path.join(RESOURCE_DIR, "seeds")
SOURCE_EVIDENCE_DIR = os.path.join(RESOURCE_DIR, "evidence")
DIAGNOSTIC_FIXTURE_DIR = os.fspath(PROJECT_ROOT / "tests" / "fixtures" / "diagnostics")
RUNTIME_DATA_DIR = os.fspath(_var_root())
RAW_DATA_DIR = os.path.join(RUNTIME_DATA_DIR, "sources")
VAR_LAYOUT_DIRS = (
    "sources",
    "snapshots",
    "state",
    "ipc",
    "cache",
    "profiles",
    "logs",
    "reports",
    "locks",
)


def get_base_dir() -> str:
    return BASE_DIR


def get_resource_dir() -> str:
    return RESOURCE_DIR


def get_var_dir() -> Path:
    return Path(RUNTIME_DATA_DIR)


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def resource_path(*parts: str) -> Path:
    return Path(RESOURCE_DIR).joinpath(*parts)


def var_path(*parts: str) -> Path:
    candidate = Path(RUNTIME_DATA_DIR).joinpath(*parts).resolve()
    root = Path(RUNTIME_DATA_DIR).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"运行态路径越界：{'/'.join(parts)}")
    return candidate


def ensure_var_layout() -> Path:
    """创建公开的运行态顶层目录；不播种数据，也不切换任何 current。"""

    root = get_var_dir()
    for dirname in VAR_LAYOUT_DIRS:
        (root / dirname).mkdir(parents=True, exist_ok=True)
    return root


__all__ = [
    "ASSET_DIR",
    "BASE_DIR",
    "BUNDLE_ROOT_DIR",
    "CHAMPION_ASSET_DIR",
    "DIAGNOSTIC_FIXTURE_DIR",
    "INDEX_DATA_DIR",
    "PROJECT_ROOT",
    "RAW_DATA_DIR",
    "RESOURCE_DIR",
    "RUNTIME_DATA_DIR",
    "SOURCE_EVIDENCE_DIR",
    "STARTUP_SEED_DIR",
    "STATIC_DATA_DIR",
    "get_base_dir",
    "get_resource_dir",
    "get_var_dir",
    "ensure_var_layout",
    "project_path",
    "resource_path",
    "var_path",
]
