from __future__ import annotations

"""打包后稳定资源播种工具。

文件职责：
- 在打包产物首次运行时，把 bundle 内置的稳定资源播种到运行目录

核心输入：
- bundle manifest
- bundle 内 `data/static`、`data/indexes`、`data/raw/hextech` 与 `assets/`

核心输出：
- 运行目录中的稳定配置和图片资源

主要依赖：
- `tools.bundle_manifest`

维护提醒：
- 稳定资源只补缺失文件；Hextech 快照只在包内文件更新时覆盖旧快照
"""

import json
import logging
import shutil
from pathlib import Path, PurePosixPath

from tools.bundle_manifest import BUNDLE_MANIFEST_NAME


HEXTECH_SNAPSHOT_PREFIX = PurePosixPath("data/raw/hextech")
HEXTECH_SNAPSHOT_PATTERN_PREFIX = "Hextech_Data_"
SYNERGY_DATA_PATH = PurePosixPath("data/raw/synergy/Champion_Synergy.json")
logger = logging.getLogger(__name__)


def _empty_manifest() -> dict:
    return {"static_files": [], "index_files": [], "asset_files": [], "hextech_snapshot_files": [], "synergy_data_file": ""}


def _load_bundle_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / BUNDLE_MANIFEST_NAME
    if not manifest_path.exists():
        return _empty_manifest()
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _empty_manifest()


def _normalize_manifest_path(relative_name: object) -> PurePosixPath | None:
    relative_text = str(relative_name).replace("\\", "/").strip()
    if not relative_text:
        return None
    relative_path = PurePosixPath(relative_text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    return relative_path


def _hextech_snapshot_path(relative_name: object) -> PurePosixPath | None:
    relative_path = _normalize_manifest_path(relative_name)
    if relative_path is None:
        return None

    if relative_path.parent == HEXTECH_SNAPSHOT_PREFIX:
        snapshot_path = relative_path
    elif len(relative_path.parts) == 1:
        # 兼容旧 manifest 若只记录文件名，运行时仍按标准目录播种。
        snapshot_path = HEXTECH_SNAPSHOT_PREFIX / relative_path.name
    else:
        return None

    name = snapshot_path.name
    if not (name.startswith(HEXTECH_SNAPSHOT_PATTERN_PREFIX) and name.endswith(".csv")):
        return None
    return snapshot_path


def _synergy_data_path(relative_name: object) -> PurePosixPath | None:
    relative_path = _normalize_manifest_path(relative_name)
    if relative_path is None:
        return None
    if relative_path == SYNERGY_DATA_PATH:
        return relative_path
    if len(relative_path.parts) == 1 and relative_path.name == SYNERGY_DATA_PATH.name:
        return SYNERGY_DATA_PATH
    return None


def _copy_if_missing_or_older(source: Path, target: Path) -> None:
    if not source.exists():
        return
    should_copy = not target.exists()
    if not should_copy:
        try:
            should_copy = source.stat().st_mtime > target.stat().st_mtime
        except OSError:
            should_copy = True
    if not should_copy:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    logger.info("已播种 bundle 快照：%s -> %s", source, target)


def seed_bundled_resources(
    *,
    bundle_root: str | Path,
    runtime_static_dir: str | Path,
    runtime_index_dir: str | Path,
    runtime_asset_dir: str | Path,
    runtime_hextech_dir: str | Path | None = None,
    runtime_synergy_dir: str | Path | None = None,
) -> None:
    """按 manifest 把 bundle 资源播种到运行目录，仅补缺失文件。"""
    bundle_base = Path(bundle_root)
    if not bundle_base.exists():
        return

    manifest = _load_bundle_manifest(bundle_base)
    static_dir = Path(runtime_static_dir)
    index_dir = Path(runtime_index_dir)
    asset_dir = Path(runtime_asset_dir)
    hextech_dir = Path(runtime_hextech_dir) if runtime_hextech_dir is not None else None
    synergy_dir = Path(runtime_synergy_dir) if runtime_synergy_dir is not None else None
    bundled_static_dir = bundle_base / "data" / "static"
    bundled_index_dir = bundle_base / "data" / "indexes"
    bundled_asset_dir = bundle_base / "assets"

    static_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    if hextech_dir is not None:
        hextech_dir.mkdir(parents=True, exist_ok=True)
    if synergy_dir is not None:
        synergy_dir.mkdir(parents=True, exist_ok=True)

    static_files = list(manifest.get("static_files", []))
    index_files = list(manifest.get("index_files", []))

    for filename in static_files:
        source = bundled_static_dir / filename
        target = static_dir / filename
        if source.exists() and not target.exists():
            shutil.copy2(source, target)

    for filename in index_files:
        source = bundled_index_dir / filename
        target = index_dir / filename
        if source.exists() and not target.exists():
            shutil.copy2(source, target)

    if hextech_dir is not None:
        for filename in manifest.get("hextech_snapshot_files", []):
            snapshot_path = _hextech_snapshot_path(filename)
            if snapshot_path is None:
                continue
            source = bundle_base / Path(*snapshot_path.parts)
            target = hextech_dir / snapshot_path.name
            _copy_if_missing_or_older(source, target)

    if synergy_dir is not None:
        synergy_path = _synergy_data_path(manifest.get("synergy_data_file", ""))
        if synergy_path is not None:
            source = bundle_base / Path(*synergy_path.parts)
            target = synergy_dir / synergy_path.name
            _copy_if_missing_or_older(source, target)

    for relative_name in manifest.get("asset_files", []):
        source = bundled_asset_dir / Path(relative_name)
        target = asset_dir / Path(relative_name)
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
