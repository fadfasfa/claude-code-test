from __future__ import annotations

"""打包白名单清单生成器。

负责枚举稳定配置、图片资源和静态页面，并生成 bundle manifest，
供构建流程和打包后运行时共同消费。
"""

import json
import shutil
from pathlib import Path
from typing import Iterable


STABLE_CONFIG_FILES = (
    "Champion_Core_Data.json",
    "Champion_Alias_Index.json",
    "Augment_Icon_Manifest.json",
    "Augment_Icon_Map.json",
    "Augment_Full_Map.json",
    "Augment_Apexlol_Map.json",
    "hero_version.txt",
)
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"


def iter_stable_asset_files(asset_dir: Path) -> Iterable[Path]:
    if not asset_dir.exists():
        return []
    return sorted(
        path for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES
    )


def build_bundle_manifest(base_dir: Path) -> dict:
    config_dir = base_dir / "config"
    asset_dir = base_dir / "assets"
    static_dir = base_dir / "display" / "static"

    config_files = [
        name for name in STABLE_CONFIG_FILES if (config_dir / name).exists()
    ]
    asset_files = [
        path.relative_to(asset_dir).as_posix()
        for path in iter_stable_asset_files(asset_dir)
    ]

    return {
        "config_files": config_files,
        "asset_files": asset_files,
        "static_dirs": ["static"] if static_dir.exists() else [],
    }


def prepare_bundle_runtime(base_dir: Path, build_dir: Path) -> Path:
    bundle_root = build_dir / "_bundle_runtime"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)

    config_dir = base_dir / "config"
    asset_dir = base_dir / "assets"
    static_dir = base_dir / "display" / "static"

    manifest = build_bundle_manifest(base_dir)
    (bundle_root / "config").mkdir(parents=True, exist_ok=True)
    (bundle_root / "assets").mkdir(parents=True, exist_ok=True)

    if static_dir.exists():
        shutil.copytree(static_dir, bundle_root / "static")

    for filename in manifest["config_files"]:
        shutil.copy2(config_dir / filename, bundle_root / "config" / filename)

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
