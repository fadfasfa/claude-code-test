from __future__ import annotations

"""Hextech 打包资源规则。

本模块只描述源文件如何进入包，不创建中间资源副本。打包脚本会把这些
规则转换为 PyInstaller 的 ``--add-data`` 参数，并把所有临时文件写入系统
临时目录。`assets/` 会按目录交给 PyInstaller，但目录内必须全部是
``ASSET_SUFFIXES`` 白名单图片，避免临时文件被静默带入发布包。
"""

from dataclasses import dataclass
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
    "augment.name-to-icon.v1.json",
)
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
HEXTECH_SNAPSHOT_DIR = Path("data") / "raw" / "hextech"
HEXTECH_SNAPSHOT_PATTERN = "Hextech_Data_*.csv"
SYNERGY_DATA_DIR = Path("data") / "raw" / "synergy"
BUNDLED_HEXTECH_SNAPSHOT_DIR = Path("resources") / "snapshots" / "hextech"
BUNDLED_SYNERGY_DATA_DIR = Path("resources") / "snapshots" / "synergy"
SYNERGY_LEGACY_FILENAME = "Champion_Synergy.json"
SYNERGY_LATEST_POINTER_FILENAME = "Champion_Synergy_latest.v1.json"
SYNERGY_SNAPSHOT_PATTERN = "Champion_Synergy_*.json"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
OVERLAY_ANCHOR_CALIBRATION_FILENAME = "overlay_anchor_calibration.v1.json"
FORBIDDEN_BUNDLE_PATH_PARTS = (
    "data/raw",
    "data/runtime",
    "data/processed",
    "runtime/cache",
    "runtime/profile",
    "runtime/log",
    "runtime/logs",
    "runtime/debug",
    OVERLAY_ANCHOR_CALIBRATION_FILENAME,
)
SOURCE_FILE_ALLOWLIST = (
    "build.py",
    "hextech_ui.py",
    "web_server.py",
    "tools/build_package.py",
    "tools/package_rules.py",
    "tools/bundle_manifest.py",
    "tools/runtime_bundle.py",
    "tools/dev_checks.py",
    "tools/acceptance/overlay_performance_probe.py",
    "tools/acceptance/probe_official_overlay_provider.py",
    "tools/acceptance/smoke_packaged_startup.py",
)


@dataclass(frozen=True)
class PackageData:
    """一条 PyInstaller data 规则。"""

    source: Path
    target: str


def web_static_dir(base_dir: Path) -> Path:
    return base_dir / "hextech" / "display" / "web" / "static"


def iter_stable_asset_files(asset_dir: Path) -> Iterable[Path]:
    if not asset_dir.exists():
        return []
    return sorted(
        path
        for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES
    )


def iter_unexpected_asset_files(asset_dir: Path) -> Iterable[Path]:
    """列出不能随 `assets/` 目录整体进入包的非白名单文件。"""

    if not asset_dir.exists():
        return []
    return sorted(
        path
        for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() not in ASSET_SUFFIXES
    )


def validate_asset_dir_for_package(asset_dir: Path) -> None:
    unexpected = list(iter_unexpected_asset_files(asset_dir))
    if not unexpected:
        return

    sample = ", ".join(path.relative_to(asset_dir).as_posix() for path in unexpected[:5])
    suffix = "" if len(unexpected) <= 5 else f" 等 {len(unexpected)} 个文件"
    raise ValueError(f"assets 目录包含非打包白名单文件：{sample}{suffix}；请清理或移出非图片文件后再打包。")


def iter_hextech_snapshot_files(base_dir: Path) -> Iterable[Path]:
    snapshot_dir = base_dir / HEXTECH_SNAPSHOT_DIR
    if not snapshot_dir.exists():
        return []
    return sorted(path for path in snapshot_dir.glob(HEXTECH_SNAPSHOT_PATTERN) if path.is_file())


def is_synergy_snapshot_file(path: Path) -> bool:
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
        path
        for path in synergy_dir.glob(SYNERGY_SNAPSHOT_PATTERN)
        if path.is_file() and is_synergy_snapshot_file(path)
    ]
    pointer = synergy_dir / SYNERGY_LATEST_POINTER_FILENAME
    if pointer.exists():
        files.append(pointer)
    legacy = synergy_dir / SYNERGY_LEGACY_FILENAME
    if legacy.exists():
        files.append(legacy)
    return sorted(files)


def iter_source_files(base_dir: Path) -> list[str]:
    """列出最终结构的源码清单；业务实现只从 `hextech/` 主应用包收口。"""

    source_files = {
        path.relative_to(base_dir).as_posix()
        for path in (base_dir / "hextech").rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    }
    source_files.update(
        relative_name for relative_name in SOURCE_FILE_ALLOWLIST
        if (base_dir / Path(relative_name)).exists()
    )
    return sorted(source_files)


def iter_package_data_entries(base_dir: Path, manifest_path: Path) -> list[PackageData]:
    """返回 PyInstaller data 规则，不复制任何业务资源到仓库内中间目录。"""

    data_static_dir = base_dir / "data" / "static"
    data_index_dir = base_dir / "data" / "indexes"
    asset_dir = base_dir / "assets"
    static_dir = web_static_dir(base_dir)
    entries: list[PackageData] = []

    if static_dir.exists():
        entries.append(PackageData(static_dir, "static"))

    for filename in STABLE_STATIC_FILES:
        source = data_static_dir / filename
        if source.exists():
            entries.append(PackageData(source, "data/static"))
    for filename in STABLE_INDEX_FILES:
        source = data_index_dir / filename
        if source.exists():
            entries.append(PackageData(source, "data/indexes"))

    for source in iter_hextech_snapshot_files(base_dir):
        entries.append(PackageData(source, BUNDLED_HEXTECH_SNAPSHOT_DIR.as_posix()))
    for source in iter_synergy_data_files(base_dir):
        entries.append(PackageData(source, BUNDLED_SYNERGY_DATA_DIR.as_posix()))

    if asset_dir.exists():
        validate_asset_dir_for_package(asset_dir)
        entries.append(PackageData(asset_dir, "assets"))

    entries.append(PackageData(manifest_path, "."))
    return entries
