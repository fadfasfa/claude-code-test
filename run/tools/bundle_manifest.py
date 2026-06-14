from __future__ import annotations

"""打包白名单清单生成器。

文件职责：
- 枚举稳定配置、静态页面、图片资源和首启可用快照
- 生成构建期与运行期共用的 bundle manifest

核心输入：
- `data/static/` 与 `data/indexes/`
- `data/raw/hextech/Hextech_Data_*.csv`
- `data/raw/synergy/Champion_Synergy_YYYYMMDD_HHMMSS.json`
- `data/raw/synergy/Champion_Synergy_latest.v1.json`
- `assets/`
- `display/static/`

核心输出：
- bundle manifest 字典
- `_bundle_runtime/` 目录结构

主要依赖：
- `shutil`
- `json`

维护提醒：
- 这里只白名单稳定资源与首启冷启动所需快照，不打包运行态缓存/锁/日志
- 若 manifest 字段有改动，必须同步检查 `tools.runtime_bundle` 与烟测脚本
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable


STABLE_STATIC_FILES = (
    "Champion_Core_Data.json",
    "Augment_Icon_Manifest.json",
    "Augment_Apexlol_Map.json",
    "Augment_Full_Map.json",
    "Augment_Icon_Map.json",
    "hero_version.txt",
)

STABLE_INDEX_FILES = (
    "Champion_Alias_Index.json",
)
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
HEXTECH_SNAPSHOT_DIR = Path("data") / "raw" / "hextech"
HEXTECH_SNAPSHOT_PATTERN = "Hextech_Data_*.csv"
SYNERGY_DATA_DIR = Path("data") / "raw" / "synergy"
SYNERGY_LEGACY_FILENAME = "Champion_Synergy.json"
SYNERGY_LATEST_POINTER_FILENAME = "Champion_Synergy_latest.v1.json"
SYNERGY_SNAPSHOT_PATTERN = "Champion_Synergy_*.json"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
OVERLAY_ANCHOR_CALIBRATION_FILENAME = "overlay_anchor_calibration.v1.json"
FORBIDDEN_BUNDLE_PATH_PARTS = (
    "data/runtime",
    OVERLAY_ANCHOR_CALIBRATION_FILENAME,
)
SOURCE_FILE_ALLOWLIST = (
    "display/game_overlay_host.py",
    "display/service_manager.py",
    "processing/overlay_event_channel.py",
    "processing/overlay_hint_cache.py",
    "processing/official_overlay_provider.py",
    "processing/overlay_vision_sidecar.py",
    "processing/ui_feature_flags.py",
    "tools/overlay_performance_probe.py",
    "tools/probe_official_overlay_provider.py",
)


def _assert_no_runtime_cache_entries(manifest: dict) -> None:
    serialized = json.dumps(manifest, ensure_ascii=False).replace("\\", "/")
    for forbidden in FORBIDDEN_BUNDLE_PATH_PARTS:
        if forbidden in serialized:
            raise ValueError(f"bundle manifest must not include runtime cache entry: {forbidden}")


def _relative_to_base(path: Path, base_dir: Path) -> str:
    return path.relative_to(base_dir).as_posix()


def iter_stable_asset_files(asset_dir: Path) -> Iterable[Path]:
    if not asset_dir.exists():
        return []
    return sorted(
        path for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES
    )


def iter_hextech_snapshot_files(base_dir: Path) -> Iterable[Path]:
    snapshot_dir = base_dir / HEXTECH_SNAPSHOT_DIR
    if not snapshot_dir.exists():
        return []
    return sorted(path for path in snapshot_dir.glob(HEXTECH_SNAPSHOT_PATTERN) if path.is_file())


def _is_synergy_snapshot_file(path: Path) -> bool:
    name = path.name
    stem = name.removeprefix("Champion_Synergy_").removesuffix(".json")
    parts = stem.split("_")
    return (
        len(parts) in {2, 3}
        and len(parts[0]) == 8
        and len(parts[1]) == 6
        and parts[0].isdigit()
        and parts[1].isdigit()
        and (len(parts) == 2 or (len(parts[2]) == 2 and parts[2].isdigit()))
    )


def iter_synergy_data_files(base_dir: Path) -> Iterable[Path]:
    """列出可打包的协同时间快照和 latest 指针，旧固定名仅作兼容兜底。"""
    synergy_dir = base_dir / SYNERGY_DATA_DIR
    if not synergy_dir.exists():
        return []

    files = [
        path for path in synergy_dir.glob(SYNERGY_SNAPSHOT_PATTERN)
        if path.is_file() and _is_synergy_snapshot_file(path)
    ]
    pointer = synergy_dir / SYNERGY_LATEST_POINTER_FILENAME
    if pointer.exists():
        files.append(pointer)
    legacy = synergy_dir / SYNERGY_LEGACY_FILENAME
    if legacy.exists():
        files.append(legacy)
    return sorted(files)


def build_bundle_manifest(base_dir: Path) -> dict:
    static_dir = base_dir / "data" / "static"
    index_dir = base_dir / "data" / "indexes"
    asset_dir = base_dir / "assets"

    static_files = [
        name for name in STABLE_STATIC_FILES if (static_dir / name).exists()
    ]
    index_files = [
        name for name in STABLE_INDEX_FILES if (index_dir / name).exists()
    ]
    asset_files = [str(path.relative_to(asset_dir)) for path in iter_stable_asset_files(asset_dir)]
    hextech_snapshot_files = [
        _relative_to_base(path, base_dir)
        for path in iter_hextech_snapshot_files(base_dir)
    ]
    synergy_data_files = [
        _relative_to_base(path, base_dir)
        for path in iter_synergy_data_files(base_dir)
    ]
    synergy_data_file = next(
        (name for name in synergy_data_files if Path(name).name.startswith("Champion_Synergy_") and Path(name).name != SYNERGY_LATEST_POINTER_FILENAME),
        "",
    )
    source_files = [
        relative_name for relative_name in SOURCE_FILE_ALLOWLIST
        if (base_dir / Path(relative_name)).exists()
    ]
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "static_files": static_files,
        "index_files": index_files,
        "asset_files": asset_files,
        "hextech_snapshot_files": hextech_snapshot_files,
        "synergy_data_files": synergy_data_files,
        "synergy_data_file": synergy_data_file,
        "source_files": source_files,
    }
    _assert_no_runtime_cache_entries(manifest)
    return manifest


def prepare_bundle_runtime(base_dir: Path, build_dir: Path) -> Path:
    """把 manifest 对应的稳定资源复制到临时 bundle 目录。"""
    bundle_root = build_dir / "_bundle_runtime"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)

    data_static_dir = base_dir / "data" / "static"
    data_index_dir = base_dir / "data" / "indexes"
    asset_dir = base_dir / "assets"
    static_dir = base_dir / "display" / "static"

    manifest = build_bundle_manifest(base_dir)
    (bundle_root / "data" / "static").mkdir(parents=True, exist_ok=True)
    (bundle_root / "data" / "indexes").mkdir(parents=True, exist_ok=True)
    (bundle_root / HEXTECH_SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
    (bundle_root / SYNERGY_DATA_DIR).mkdir(parents=True, exist_ok=True)
    (bundle_root / "assets").mkdir(parents=True, exist_ok=True)

    if static_dir.exists():
        shutil.copytree(static_dir, bundle_root / "static")

    for filename in manifest["static_files"]:
        shutil.copy2(data_static_dir / filename, bundle_root / "data" / "static" / filename)
    for filename in manifest["index_files"]:
        shutil.copy2(data_index_dir / filename, bundle_root / "data" / "indexes" / filename)

    for relative_name in manifest["hextech_snapshot_files"]:
        source = base_dir / Path(relative_name)
        target = bundle_root / Path(relative_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    synergy_relative_names = list(manifest.get("synergy_data_files") or [])
    legacy_single = str(manifest.get("synergy_data_file", "")).strip()
    if legacy_single and legacy_single not in synergy_relative_names:
        synergy_relative_names.append(legacy_single)
    for synergy_relative_name in synergy_relative_names:
        source = base_dir / Path(synergy_relative_name)
        target = bundle_root / Path(synergy_relative_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    for relative_name in manifest["asset_files"]:
        source = asset_dir / Path(relative_name)
        target = bundle_root / "assets" / Path(relative_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    (bundle_root / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return bundle_root
