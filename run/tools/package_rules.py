"""Hextech 打包资源规则。

本模块只描述源文件如何进入包，不创建中间资源副本。打包脚本会把这些
规则转换为 PyInstaller 的 ``--add-data`` 参数，并把所有临时文件写入系统
临时目录。`data/static/assets/` 会按目录交给 PyInstaller 的 `data/static/assets`
目标，但目录内必须全部是
``ASSET_SUFFIXES`` 白名单图片，避免临时文件被静默带入发布包。

调用方: build_package、bundle_manifest、dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


STABLE_STATIC_FILES = (
    "英雄目录.v1.json",
    "海克斯资源目录.v1.json",
    "Champion_Synergy_Cleaned.json",
    "hero_version.txt",
)
STABLE_INDEX_FILES = ()
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ASSET_DOCUMENTATION_FILES = {"README.md"}
DATA_ROOT_DIR = Path("data")
STATIC_ROOT_DIR = DATA_ROOT_DIR / "static"
DATA_STATIC_ASSET_DIR = STATIC_ROOT_DIR / "assets"
DATA_STATIC_VERSION_DIR = STATIC_ROOT_DIR / "version"
DATA_STARTUP_SEED_DIR = DATA_ROOT_DIR / "seed" / "startup"
DATA_STARTUP_SNAPSHOT_DIR = DATA_STARTUP_SEED_DIR / "snapshots"
DATA_DIAGNOSTIC_FIXTURE_DIR = DATA_ROOT_DIR / "fixtures" / "diagnostics"
DATA_SOURCE_EVIDENCE_DIR = DATA_ROOT_DIR / "evidence"
HEXTECH_SNAPSHOT_DIR = DATA_STARTUP_SEED_DIR / "hextech"
HEXTECH_SNAPSHOT_PATTERN = "Hextech_Data_*.csv"
HEXTECH_SNAPSHOT_FILENAME_RE = re.compile(r"^Hextech_Data_\d{4}-\d{2}-\d{2}\.csv$")
SYNERGY_DATA_DIR = DATA_STARTUP_SEED_DIR / "synergy"
BUNDLED_VERSION_DATA_DIR = DATA_STATIC_VERSION_DIR
BUNDLED_ASSET_DIR = DATA_STATIC_ASSET_DIR
BUNDLED_HEXTECH_SNAPSHOT_DIR = DATA_STARTUP_SEED_DIR / "hextech"
BUNDLED_SYNERGY_DATA_DIR = SYNERGY_DATA_DIR
BUNDLED_SNAPSHOT_SEED_DIR = DATA_STARTUP_SNAPSHOT_DIR
SYNERGY_LEGACY_FILENAME = "Champion_Synergy.json"
SYNERGY_LATEST_POINTER_FILENAME = "Champion_Synergy_latest.v1.json"
SYNERGY_SNAPSHOT_PATTERN = "Champion_Synergy_*.json"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
OVERLAY_ANCHOR_CALIBRATION_FILENAME = "overlay_anchor_calibration.v1.json"
FORBIDDEN_BUNDLE_PATH_PARTS = (
    "data/raw",
    "data/runtime",
    "data/processed",
    "resources/",
    "runtime/cache",
    "runtime/profile",
    "runtime/log",
    "runtime/logs",
    "runtime/debug",
    "runtime/report",
    "runtime/reports",
    "__pycache__",
    ".pyc",
    ".pyo",
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
    "tools/acceptance/overlay_performance_probe.py",
    "tools/acceptance/probe_official_overlay_provider.py",
    "tools/acceptance/smoke_packaged_startup.py",
)
FORBIDDEN_SOURCE_PREFIXES = (
    "run/tests/",
    "tests/",
    "tools/diagnostics/",
    "tools/maintenance/",
)
FORBIDDEN_SOURCE_FILES = {
    "tools/collect_runtime_diagnostics.py",
    "tools/cleanup_runtime.py",
    "tools/dev_checks.py",
    "tools/migrate_runtime_data.py",
}


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
    """列出不能随 data/static/assets 整体进入包的非白名单文件。"""

    if not asset_dir.exists():
        return []
    return sorted(
        path
        for path in asset_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in ASSET_SUFFIXES
        and path.name not in ASSET_DOCUMENTATION_FILES
    )


def validate_asset_dir_for_package(asset_dir: Path) -> None:
    unexpected = list(iter_unexpected_asset_files(asset_dir))
    if not unexpected:
        return

    sample = ", ".join(path.relative_to(asset_dir).as_posix() for path in unexpected[:5])
    suffix = "" if len(unexpected) <= 5 else f" 等 {len(unexpected)} 个文件"
    raise ValueError(f"data/static/assets 目录包含非打包白名单文件：{sample}{suffix}；请清理或移出非图片文件后再打包。")


def iter_hextech_snapshot_files(base_dir: Path) -> Iterable[Path]:
    snapshot_dir = base_dir / HEXTECH_SNAPSHOT_DIR
    if not snapshot_dir.exists():
        return []
    return sorted(
        path
        for path in snapshot_dir.glob(HEXTECH_SNAPSHOT_PATTERN)
        if path.is_file() and HEXTECH_SNAPSHOT_FILENAME_RE.match(path.name)
    )


def validate_source_file_boundary(relative_name: str) -> None:
    normalized = str(relative_name).replace("\\", "/")
    if normalized in FORBIDDEN_SOURCE_FILES or any(normalized.startswith(prefix) for prefix in FORBIDDEN_SOURCE_PREFIXES):
        raise ValueError(f"开发/诊断工具不得进入打包源码清单：{normalized}")
    if "__pycache__" in normalized or normalized.endswith((".pyc", ".pyo")):
        raise ValueError(f"Python 生成物不得进入打包源码清单：{normalized}")


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


def iter_snapshot_seed_files(snapshot_root: Path) -> list[Path]:
    """列出外部已验证 generation 根中的普通文件，不跟随目录外路径。"""

    root = snapshot_root.resolve()
    if not (root / "current.v1.json").is_file():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and root in path.resolve().parents
    )


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
    for relative_name in source_files:
        validate_source_file_boundary(relative_name)
    return sorted(source_files)


def iter_package_data_entries(
    base_dir: Path,
    manifest_path: Path,
    *,
    verified_snapshot_root: Path | None = None,
) -> list[PackageData]:
    """返回 PyInstaller data 规则，不复制任何业务资源到仓库内中间目录。"""

    data_static_dir = base_dir / DATA_STATIC_VERSION_DIR
    data_index_dir = base_dir / DATA_STATIC_VERSION_DIR
    asset_dir = base_dir / DATA_STATIC_ASSET_DIR
    static_dir = web_static_dir(base_dir)
    entries: list[PackageData] = []

    if static_dir.exists():
        entries.append(PackageData(static_dir, "static"))

    for filename in STABLE_STATIC_FILES:
        source = data_static_dir / filename
        if source.exists():
            entries.append(PackageData(source, BUNDLED_VERSION_DATA_DIR.as_posix()))
    for filename in STABLE_INDEX_FILES:
        source = data_index_dir / filename
        if source.exists():
            entries.append(PackageData(source, BUNDLED_VERSION_DATA_DIR.as_posix()))

    for source in iter_hextech_snapshot_files(base_dir):
        entries.append(PackageData(source, BUNDLED_HEXTECH_SNAPSHOT_DIR.as_posix()))
    for source in iter_synergy_data_files(base_dir):
        entries.append(PackageData(source, BUNDLED_SYNERGY_DATA_DIR.as_posix()))
    if verified_snapshot_root is not None:
        snapshot_root = verified_snapshot_root.resolve()
        for source in iter_snapshot_seed_files(snapshot_root):
            relative_parent = source.relative_to(snapshot_root).parent
            target = BUNDLED_SNAPSHOT_SEED_DIR / relative_parent
            entries.append(PackageData(source, target.as_posix()))

    if asset_dir.exists():
        validate_asset_dir_for_package(asset_dir)
        entries.append(PackageData(asset_dir, BUNDLED_ASSET_DIR.as_posix()))

    entries.append(PackageData(manifest_path, "."))
    return entries
