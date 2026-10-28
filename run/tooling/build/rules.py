"""发布包资源白名单。

发布包只包含只读 ``resources`` 和 bundle manifest。可写
``var``、测试 fixture、诊断证据与 tooling 都不得进入应用包。
"""

from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tooling.build.resource_manifest import MANIFEST_RELATIVE_PATH, validate_resource_manifest
from tooling.build.cohort_seed import collect_cohort_seed


CATALOG_FILES = (
    "manifest.v2.json",
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    base_dir = base_dir.resolve()
    seed_root = (verified_snapshot_root or (base_dir / SEED_DIR / "runtime.v4" / "snapshots")).resolve()
    entries: list[PackageData] = []
    report = validate_resource_manifest(base_dir)
    for rel_path in report["packaged_files"]:
        if rel_path.startswith(SEED_DIR.as_posix() + "/"):
            continue
        entries.append(PackageData(base_dir / rel_path, Path(rel_path).parent.as_posix()))
    try:
        bundle_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bundle manifest unreadable") from exc
    expected_hashes = bundle_manifest.get("seed_sha256")
    if not isinstance(expected_hashes, dict):
        raise ValueError("bundle manifest lacks v4 seed hashes")
    seed = collect_cohort_seed(seed_root)
    names = [seed.bundled_name(path) for path in seed.files]
    if set(names) != set(expected_hashes):
        raise ValueError("verified release closure differs from bundle manifest")
    for source, name in zip(seed.files, names):
        if _sha256(source) != expected_hashes[name]:
            raise ValueError(f"verified seed hash mismatch: {name}")
        entries.append(PackageData(source, Path(name).parent.as_posix()))

    entries.append(PackageData(base_dir / MANIFEST_RELATIVE_PATH, RESOURCE_ROOT_DIR.as_posix()))
    entries.append(PackageData(manifest_path, "."))
    return entries


def stage_package_data_entries(entries: Iterable[PackageData], staging_root: Path) -> list[PackageData]:
    """把所有 PyInstaller data 输入复制到仓库外 clean staging。"""

    root = staging_root.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    staged: list[PackageData] = []
    for index, entry in enumerate(entries):
        target_root = root / f"entry-{index:04d}"
        if entry.source.is_dir():
            destination = target_root / entry.source.name
            shutil.copytree(entry.source, destination)
        else:
            target_root.mkdir(parents=True)
            destination = target_root / entry.source.name
            shutil.copy2(entry.source, destination)
        staged.append(PackageData(destination, entry.target))
    return staged


def stage_package_data_tree(entries: Iterable[PackageData], staging_root: Path) -> PackageData:
    """把白名单资源合并为一棵 clean tree，避免 Windows 命令行过长。

    输入仍是逐文件 manifest 结果；这里不遍历仓库 assets，未列文件不会
    被复制到 staging，PyInstaller 只看到最终的白名单树。
    """

    root = staging_root.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for entry in entries:
        target_root = (root / entry.target).resolve()
        if target_root != root and root not in target_root.parents:
            raise ValueError(f"打包 staging 目标越界：{entry.target}")
        if entry.source.is_dir():
            destination = target_root
            if destination.exists() and any(destination.iterdir()):
                raise ValueError(f"打包 staging 目录冲突：{entry.target}")
            shutil.copytree(entry.source, destination, dirs_exist_ok=True)
        else:
            target_root.mkdir(parents=True, exist_ok=True)
            destination = target_root / entry.source.name
            if destination.exists():
                raise ValueError(f"打包 staging 文件冲突：{destination.relative_to(root).as_posix()}")
            shutil.copy2(entry.source, destination)
    return PackageData(root, ".")
