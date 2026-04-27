from __future__ import annotations

"""打包后稳定资源播种工具。

文件职责：
- 在打包产物首次运行时，把 bundle 内置的稳定资源播种到运行目录

核心输入：
- bundle manifest
- bundle 内 `data/static`、`data/indexes` 与 `assets/`

核心输出：
- 运行目录中的稳定配置和图片资源

主要依赖：
- `tools.bundle_manifest`

维护提醒：
- 只补缺失文件，不覆盖运行中已生成的新文件
"""

import json
import shutil
from pathlib import Path

from tools.bundle_manifest import BUNDLE_MANIFEST_NAME


def _load_bundle_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / BUNDLE_MANIFEST_NAME
    if not manifest_path.exists():
        return {"static_files": [], "index_files": [], "asset_files": []}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"static_files": [], "index_files": [], "asset_files": []}


def seed_bundled_resources(
    *,
    bundle_root: str | Path,
    runtime_static_dir: str | Path,
    runtime_index_dir: str | Path,
    runtime_asset_dir: str | Path,
) -> None:
    """按 manifest 把 bundle 稳定资源播种到运行目录，仅补缺失文件。"""
    bundle_base = Path(bundle_root)
    if not bundle_base.exists():
        return

    manifest = _load_bundle_manifest(bundle_base)
    static_dir = Path(runtime_static_dir)
    index_dir = Path(runtime_index_dir)
    asset_dir = Path(runtime_asset_dir)
    bundled_static_dir = bundle_base / "data" / "static"
    bundled_index_dir = bundle_base / "data" / "indexes"
    bundled_asset_dir = bundle_base / "assets"

    static_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)

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

    for relative_name in manifest.get("asset_files", []):
        source = bundled_asset_dir / Path(relative_name)
        target = asset_dir / Path(relative_name)
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
