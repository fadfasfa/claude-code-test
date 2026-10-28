"""Hextech 可提交资源和本机运行态的唯一路径事实源。

源码态只读资源位于 ``run/resources``，可写运行态位于 ``run/var``；冻结态
资源来自 PyInstaller bundle；installed 使用用户目录 ``HextechNexus/var``，
isolated_candidate 仅使用 EXE 同级 ``var``。冻结态 profile 必须由 bundle manifest 明确声明。
本模块不提供旧 ``data`` 目录回退，路径切换失败必须显式暴露。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


class RuntimeProfileError(RuntimeError):
    """冻结包缺少可信运行态配置或候选路径发生逃逸。"""


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
    if getattr(sys, "frozen", False):
        if not hasattr(sys, "_MEIPASS"):
            raise RuntimeProfileError("冻结包缺少 bundle root，无法确定 runtime_profile")
        return Path(sys._MEIPASS).resolve()
    return _source_project_root()


def _packaged_runtime_profile() -> str:
    manifest_path = _bundle_root() / "bundle_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeProfileError(f"无法读取冻结包 runtime_profile：{manifest_path}") from exc
    profile = manifest.get("runtime_profile") if isinstance(manifest, dict) else None
    if not isinstance(profile, str) or profile not in {"installed", "isolated_candidate"}:
        raise RuntimeProfileError(f"冻结包 runtime_profile 缺失或无效：{manifest_path}")
    return profile


def _validate_candidate_root(root: Path, *, include_layout: bool = False) -> Path:
    # resolve() 会跟随 Windows junction 和 symlink。初始化时检查公开顶层目录，
    # 后续只复查根本身，具体访问由 var_path 校验，避免每次取根都扫描整个布局。
    # 验证本身及后续 mkdir 的异常都必须直接暴露。
    if root.resolve() != root:
        raise RuntimeProfileError(f"候选运行态目录发生重解析逃逸：{root}")
    if include_layout:
        for dirname in VAR_LAYOUT_DIRS:
            child = root / dirname
            if child.resolve() != child:
                raise RuntimeProfileError(f"候选运行态目录发生重解析逃逸：{child}")
    return root


def _var_root() -> Path:
    if getattr(sys, "frozen", False):
        if RUNTIME_PROFILE == "isolated_candidate":
            return _validate_candidate_root(Path(sys.executable).resolve().parent / "var", include_layout=True)
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
VAR_LAYOUT_DIRS = (
    "catalog",
    "sources",
    "snapshots",
    "state",
    "user-data",
    "cache",
    "profiles",
    "logs",
    "reports",
    "locks",
)
RUNTIME_PROFILE = _packaged_runtime_profile() if getattr(sys, "frozen", False) else "source"
RUNTIME_DATA_DIR = os.fspath(_var_root())
RAW_DATA_DIR = os.path.join(RUNTIME_DATA_DIR, "sources")


def get_base_dir() -> str:
    return BASE_DIR


def get_resource_dir() -> str:
    return RESOURCE_DIR


def get_var_dir() -> Path:
    root = Path(RUNTIME_DATA_DIR)
    if RUNTIME_PROFILE == "isolated_candidate":
        return _validate_candidate_root(root)
    return root


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def resource_path(*parts: str) -> Path:
    return Path(RESOURCE_DIR).joinpath(*parts)


def var_path(*parts: str) -> Path:
    root = get_var_dir().resolve()
    candidate = root.joinpath(*parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"运行态路径越界：{'/'.join(parts)}")
    return candidate


def ensure_var_layout() -> Path:
    """创建公开的运行态顶层目录；不播种数据，也不切换任何 current。"""

    root = get_var_dir()
    for dirname in VAR_LAYOUT_DIRS:
        var_path(dirname).mkdir(parents=True, exist_ok=True)
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
    "RUNTIME_PROFILE",
    "RuntimeProfileError",
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
