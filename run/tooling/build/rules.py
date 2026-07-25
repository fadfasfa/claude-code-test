"""发布包资源白名单。

发布包只包含只读 ``resources``、Web 静态文件和 bundle manifest。可写
``var``、测试 fixture、诊断证据与 tooling 都不得进入应用包。
"""

from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tooling.build.resource_manifest import MANIFEST_RELATIVE_PATH, load_resource_manifest, validate_resource_manifest


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
    pointer = root / "current.v2.json"
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_descriptors(base_dir: Path) -> dict[str, dict]:
    payload = load_resource_manifest(base_dir)
    items = payload.get("files")
    if not isinstance(items, list):
        raise ValueError("resources manifest v2 缺少 files")
    return {
        str(item.get("path") or ""): item
        for item in items
        if isinstance(item, dict) and str(item.get("path") or "")
    }


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
    seed_root = (verified_snapshot_root or (base_dir / SEED_DIR)).resolve()
    entries: list[PackageData] = []
    static_dir = web_static_dir(base_dir)
    if static_dir.is_dir():
        entries.append(PackageData(static_dir, "static"))

    report = validate_resource_manifest(base_dir)
    descriptors = _manifest_descriptors(base_dir)
    packaged_files = report["packaged_files"]
    seed_prefix = SEED_DIR.as_posix() + "/"
    for rel_path in packaged_files:
        if verified_snapshot_root is not None and rel_path.startswith(seed_prefix):
            continue
        source = base_dir / rel_path
        if rel_path.startswith(seed_prefix):
            seed_relative = Path(rel_path).relative_to(SEED_DIR)
            source = seed_root / seed_relative
            if not source.is_file():
                raise ValueError(f"verified seed 缺少 manifest 白名单文件：{seed_relative.as_posix()}")
            expected_hash = str(descriptors[rel_path].get("sha256") or "")
            if _sha256(source) != expected_hash:
                raise ValueError(f"verified seed 与 manifest SHA-256 不一致：{seed_relative.as_posix()}")
        entries.append(PackageData(source, Path(rel_path).parent.as_posix()))

    if verified_snapshot_root is not None:
        try:
            bundle_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bundle manifest 无法读取：{manifest_path}") from exc
        expected_hashes = bundle_manifest.get("seed_sha256") if isinstance(bundle_manifest, dict) else None
        if not isinstance(expected_hashes, dict):
            raise ValueError("bundle manifest 缺少 verified seed 哈希")
        verified_files = iter_seed_files(seed_root)
        bundled_names = [(SEED_DIR / path.relative_to(seed_root)).as_posix() for path in verified_files]
        if set(bundled_names) != set(str(key) for key in expected_hashes):
            raise ValueError("verified seed 文件集与 bundle manifest 不一致")
        for source, bundled_name in zip(verified_files, bundled_names):
            if _sha256(source) != str(expected_hashes[bundled_name]):
                raise ValueError(f"verified seed 与 bundle manifest SHA-256 不一致：{bundled_name}")
            entries.append(PackageData(source, Path(bundled_name).parent.as_posix()))

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
