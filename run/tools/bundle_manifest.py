from __future__ import annotations

"""打包白名单清单生成器。

文件职责：
- 枚举稳定配置、静态页面和图片资源
- 生成构建期与运行期共用的 bundle manifest

核心输入：
- `data/static/` 与 `data/indexes/`
- `assets/`
- `display/static/`

核心输出：
- bundle manifest 字典
- `_bundle_runtime/` 目录结构

主要依赖：
- `shutil`
- `json`

维护提醒：
- 这里只白名单稳定资源，不应把高频运行态文件误打进包里
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
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"


def iter_stable_asset_files(asset_dir: Path) -> Iterable[Path]:
    if not asset_dir.exists():
        return []
    return sorted(
        path for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES
    )


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

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "static_files": static_files,
        "index_files": index_files,
        "asset_files": asset_files,
    }


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
    (bundle_root / "assets").mkdir(parents=True, exist_ok=True)

    if static_dir.exists():
        shutil.copytree(static_dir, bundle_root / "static")

    for filename in manifest["static_files"]:
        shutil.copy2(data_static_dir / filename, bundle_root / "data" / "static" / filename)
    for filename in manifest["index_files"]:
        shutil.copy2(data_index_dir / filename, bundle_root / "data" / "indexes" / filename)

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
