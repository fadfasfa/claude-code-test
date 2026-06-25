from __future__ import annotations

"""打包白名单 manifest 生成器。

文件职责：
- 枚举稳定配置、静态页面、图片资源和首启可用快照
- 生成运行期 seed 使用的 bundle manifest；构建期只把这份 manifest 随包写入，不复制资源

核心输入：
- `resources/版本数据/`
- `data/raw/hextech/Hextech_Data_*.csv`（构建期源）
- `resources/首启快照/Champion_Synergy_YYYYMMDD_HHMMSS.json`（构建期源）
- `resources/首启快照/Champion_Synergy_latest.v1.json`（构建期源）
- `resources/图片资源/`
- `hextech/display/web/static/`

核心输出：
- bundle manifest 字典

主要依赖：
- `json`
- `tools.package_rules`

维护提醒：
- 这里只白名单稳定资源与首启冷启动所需快照，不打包运行态缓存/锁/日志
- 构建期可读取 `data/raw` 快照源，但 manifest 路径必须写入 `resources/snapshots`
- 若 manifest 字段有改动，必须同步检查 `tools.runtime_bundle` 与烟测脚本
"""

import json
from datetime import datetime
from pathlib import Path

from tools.package_rules import (
    BUNDLED_HEXTECH_SNAPSHOT_DIR,
    BUNDLED_SYNERGY_DATA_DIR,
    BUNDLE_MANIFEST_NAME,
    FORBIDDEN_BUNDLE_PATH_PARTS,
    HEXTECH_SNAPSHOT_DIR,
    RESOURCE_IMAGE_DIR,
    RESOURCE_VERSION_DATA_DIR,
    STABLE_INDEX_FILES,
    STABLE_STATIC_FILES,
    SYNERGY_DATA_DIR,
    SYNERGY_LATEST_POINTER_FILENAME,
    iter_hextech_snapshot_files,
    iter_source_files,
    iter_stable_asset_files,
    iter_synergy_data_files,
)


def _assert_no_runtime_cache_entries(manifest: dict) -> None:
    serialized = json.dumps(manifest, ensure_ascii=False).replace("\\", "/")
    for forbidden in FORBIDDEN_BUNDLE_PATH_PARTS:
        # TODO: manifest 字段扩展后改为按路径分量精确匹配，避免 source 文件名偶然含禁词时误报。
        if forbidden in serialized:
            raise ValueError(f"bundle manifest must not include runtime cache entry: {forbidden}")


def _relative_to_base(path: Path, base_dir: Path) -> str:
    return path.relative_to(base_dir).as_posix()


def _bundled_snapshot_name(path: Path, target_dir: Path) -> str:
    return (target_dir / path.name).as_posix()


def build_bundle_manifest(base_dir: Path) -> dict:
    static_dir = base_dir / RESOURCE_VERSION_DATA_DIR
    index_dir = base_dir / RESOURCE_VERSION_DATA_DIR
    asset_dir = base_dir / RESOURCE_IMAGE_DIR

    static_files = [
        name for name in STABLE_STATIC_FILES if (static_dir / name).exists()
    ]
    index_files = [
        name for name in STABLE_INDEX_FILES if (index_dir / name).exists()
    ]
    asset_files = [str(path.relative_to(asset_dir)) for path in iter_stable_asset_files(asset_dir)]
    hextech_snapshot_files = [
        _bundled_snapshot_name(path, BUNDLED_HEXTECH_SNAPSHOT_DIR)
        for path in iter_hextech_snapshot_files(base_dir)
    ]
    synergy_data_files = [
        _bundled_snapshot_name(path, BUNDLED_SYNERGY_DATA_DIR)
        for path in iter_synergy_data_files(base_dir)
    ]
    synergy_data_file = next(
        (name for name in synergy_data_files if Path(name).name.startswith("Champion_Synergy_") and Path(name).name != SYNERGY_LATEST_POINTER_FILENAME),
        "",
    )
    source_files = iter_source_files(base_dir)
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
