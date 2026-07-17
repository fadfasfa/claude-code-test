"""发布包资源白名单。

发布包只包含只读 ``resources``、Web 静态文件和 bundle manifest。可写
``var``、测试 fixture、诊断证据与 tooling 都不得进入应用包。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CATALOG_FILES = (
    "英雄目录.v1.json",
    "海克斯资源目录.v1.json",
    "hero_version.txt",
)
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ASSET_DOCUMENTATION_FILES = {"README.md"}
RESOURCE_ROOT_DIR = Path("resources")
CATALOG_DIR = RESOURCE_ROOT_DIR / "catalog"
ASSET_DIR = RESOURCE_ROOT_DIR / "assets"
SEED_DIR = RESOURCE_ROOT_DIR / "seeds"
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
OVERLAY_ANCHOR_CALIBRATION_FILENAME = "overlay_anchor_calibration.v1.json"
FORBIDDEN_BUNDLE_PATH_PARTS = (
    "var",
    "data/raw",
    "data/runtime",
    "data/static",
    "data/seed",
    "tests",
    "tooling",
    "profiles",
    "logs",
    "reports",
    "cache",
    "__pycache__",
    ".pyc",
    ".pyo",
    OVERLAY_ANCHOR_CALIBRATION_FILENAME,
)


@dataclass(frozen=True)
class PackageData:
    """一条 PyInstaller ``--add-data`` 规则。"""

    source: Path
    target: str


def web_static_dir(base_dir: Path) -> Path:
    return base_dir / "src" / "hextech" / "interfaces" / "web" / "backend" / "static"


def iter_stable_asset_files(asset_dir: Path) -> Iterable[Path]:
    if not asset_dir.exists():
        return []
    return sorted(path for path in asset_dir.rglob("*") if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES)


def iter_unexpected_asset_files(asset_dir: Path) -> Iterable[Path]:
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
    raise ValueError(f"resources/assets 包含非图片文件：{sample}{suffix}")


def iter_seed_files(seed_root: Path) -> list[Path]:
    """只枚举 current 指向的完整 seed generation。"""

    root = seed_root.resolve()
    pointer = root / "current.v1.json"
    if not pointer.is_file():
        return []
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        generation_id = str(payload.get("current_generation_id") or "") if isinstance(payload, dict) else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    generation_dir = (root / "generations" / generation_id).resolve()
    if not generation_id or generation_dir.parent != (root / "generations").resolve() or not generation_dir.is_dir():
        return []
    generation_files = sorted(
        path
        for path in generation_dir.rglob("*")
        if path.is_file()
        and generation_dir in path.resolve().parents
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in {".pyc", ".pyo"}
    )
    return [pointer, *generation_files]


def iter_source_files(base_dir: Path) -> list[str]:
    """生成应用源码审计清单，入口只来自 ``src/hextech``。"""

    source_root = base_dir / "src" / "hextech"
    return sorted(
        path.relative_to(base_dir).as_posix()
        for path in source_root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def iter_package_data_entries(
    base_dir: Path,
    manifest_path: Path,
    *,
    verified_snapshot_root: Path | None = None,
) -> list[PackageData]:
    """返回新布局的打包数据规则，不创建仓库内中间副本。"""

    seed_root = (verified_snapshot_root or (base_dir / SEED_DIR)).resolve()
    entries: list[PackageData] = []
    static_dir = web_static_dir(base_dir)
    if static_dir.is_dir():
        entries.append(PackageData(static_dir, "static"))

    catalog_dir = base_dir / CATALOG_DIR
    for filename in CATALOG_FILES:
        source = catalog_dir / filename
        if source.is_file():
            entries.append(PackageData(source, CATALOG_DIR.as_posix()))

    asset_dir = base_dir / ASSET_DIR
    if asset_dir.is_dir():
        validate_asset_dir_for_package(asset_dir)
        entries.append(PackageData(asset_dir, ASSET_DIR.as_posix()))

    for source in iter_seed_files(seed_root):
        relative_parent = source.relative_to(seed_root).parent
        entries.append(PackageData(source, (SEED_DIR / relative_parent).as_posix()))

    entries.append(PackageData(manifest_path, "."))
    return entries
