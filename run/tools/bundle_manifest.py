"""打包白名单 manifest 生成器。

文件职责：
- 枚举稳定配置、静态页面、图片资源和首启可用快照
- 生成运行期 seed 使用的 bundle manifest；构建期只把这份 manifest 随包写入，不复制资源

核心输入：
- `data/static/version/`
- `data/seed/startup/hextech/Hextech_Data_*.csv`（构建期源）
- `data/seed/startup/synergy/Champion_Synergy_YYYYMMDD_HHMMSS.json`（构建期源）
- `data/seed/startup/synergy/Champion_Synergy_latest.v1.json`（构建期源）
- `data/static/assets/`
- `hextech/display/web/static/`

核心输出：
- bundle manifest 字典

主要依赖：
- `json`
- `tools.package_rules`

维护提醒：
- 这里只白名单稳定资源与首启冷启动所需快照，不打包运行态缓存/锁/日志
- 构建期只读取 `data/seed/startup` 快照源，manifest 路径必须写入 `data/seed/startup`
- 若 manifest 字段有改动，必须同步检查 `tools.runtime_bundle` 与烟测脚本

调用方: tests.test_bundle_manifest、build_package、dev_checks; 关键依赖: package_rules。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
import csv

from tools.package_rules import (
    BUNDLED_HEXTECH_SNAPSHOT_DIR,
    BUNDLED_SYNERGY_DATA_DIR,
    BUNDLE_MANIFEST_NAME,
    DATA_STATIC_ASSET_DIR,
    DATA_STATIC_VERSION_DIR,
    FORBIDDEN_BUNDLE_PATH_PARTS,
    HEXTECH_SNAPSHOT_DIR,
    STABLE_INDEX_FILES,
    STABLE_STATIC_FILES,
    SYNERGY_DATA_DIR,
    SYNERGY_LATEST_POINTER_FILENAME,
    iter_hextech_snapshot_files,
    iter_source_files,
    iter_stable_asset_files,
    iter_synergy_data_files,
)


def _iter_manifest_path_strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_manifest_path_strings(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_manifest_path_strings(item)
        return
    if isinstance(value, str):
        normalized = value.replace("\\", "/").strip()
        if normalized:
            yield normalized


def _path_contains_forbidden_part(path_value: str, forbidden: str) -> bool:
    normalized = path_value.replace("\\", "/").strip("/")
    forbidden_norm = forbidden.replace("\\", "/").strip("/")
    if not normalized or not forbidden_norm:
        return False
    if forbidden_norm in {".pyc", ".pyo"}:
        return normalized.endswith(forbidden_norm)
    path_parts = PurePosixPath(normalized).parts
    forbidden_parts = PurePosixPath(forbidden_norm).parts
    if len(forbidden_parts) == 1:
        return forbidden_parts[0] in path_parts or PurePosixPath(normalized).name == forbidden_parts[0]
    return any(
        tuple(path_parts[index : index + len(forbidden_parts)]) == forbidden_parts
        for index in range(0, len(path_parts) - len(forbidden_parts) + 1)
    )


def manifest_contains_forbidden_path(manifest: dict, forbidden: str) -> bool:
    return any(_path_contains_forbidden_part(path_value, forbidden) for path_value in _iter_manifest_path_strings(manifest))


def _assert_no_runtime_cache_entries(manifest: dict) -> None:
    for forbidden in FORBIDDEN_BUNDLE_PATH_PARTS:
        if manifest_contains_forbidden_path(manifest, forbidden):
            raise ValueError(f"bundle manifest must not include runtime cache entry: {forbidden}")


REQUIRED_HEXTECH_SEED_COLUMNS = (
    "英雄名称",
    "海克斯名称",
    "英雄胜率",
    "英雄出场率",
    "海克斯胜率",
    "海克斯出场率",
)
DEFAULT_MIN_HEXTECH_SEED_ROWS = 1000
DEFAULT_MIN_HEXTECH_SEED_HEROES = 20


def _count_csv_rows_and_heroes(path: Path) -> tuple[int, int, list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        hero_column = "英雄 ID" if "英雄 ID" in columns else "英雄ID" if "英雄ID" in columns else "英雄名称"
        hero_values: set[str] = set()
        rows = 0
        for row in reader:
            rows += 1
            hero_value = str(row.get(hero_column, "") or "").strip()
            if hero_value:
                hero_values.add(hero_value)
    return rows, len(hero_values), columns


def validate_hextech_seed_health(
    base_dir: Path,
    *,
    min_rows: int = DEFAULT_MIN_HEXTECH_SEED_ROWS,
    min_heroes: int = DEFAULT_MIN_HEXTECH_SEED_HEROES,
) -> dict[str, Any]:
    """校验首启 Hextech seed 至少有一个可用快照，防止空包发布。"""

    candidates = list(iter_hextech_snapshot_files(base_dir))
    if not candidates:
        raise ValueError("missing Hextech_Data_YYYY-MM-DD.csv seed snapshot")

    failures: list[str] = []
    for path in sorted(candidates, key=lambda item: item.name, reverse=True):
        try:
            rows, unique_heroes, columns = _count_csv_rows_and_heroes(path)
        except OSError as exc:
            failures.append(f"{path.name}: read_failed={type(exc).__name__}")
            continue
        missing_columns = [column for column in REQUIRED_HEXTECH_SEED_COLUMNS if column not in columns]
        if missing_columns:
            failures.append(f"{path.name}: missing_columns={','.join(missing_columns)}")
            continue
        if rows < min_rows:
            failures.append(f"{path.name}: rows={rows} < {min_rows}")
            continue
        if unique_heroes < min_heroes:
            failures.append(f"{path.name}: unique_heroes={unique_heroes} < {min_heroes}")
            continue
        return {
            "valid": True,
            "path": path.relative_to(base_dir).as_posix(),
            "filename": path.name,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "rows": rows,
            "unique_heroes": unique_heroes,
            "required_columns": list(REQUIRED_HEXTECH_SEED_COLUMNS),
        }

    raise ValueError("no healthy Hextech seed snapshot: " + "; ".join(failures[:3]))


def validate_bundle_manifest(manifest: dict) -> None:
    """校验 manifest 关键字段非空，并保持路径边界。"""

    missing: list[str] = []
    for field in ("static_files", "hextech_snapshot_files", "synergy_data_files", "source_files"):
        value = manifest.get(field)
        if not isinstance(value, list) or not value:
            missing.append(field)
    if not str(manifest.get("synergy_data_file", "") or "").strip():
        missing.append("synergy_data_file")

    if missing:
        raise ValueError("bundle manifest missing critical fields: " + ", ".join(missing))

    _assert_no_runtime_cache_entries(manifest)


def _relative_to_base(path: Path, base_dir: Path) -> str:
    return path.relative_to(base_dir).as_posix()


def _bundled_snapshot_name(path: Path, target_dir: Path) -> str:
    return (target_dir / path.name).as_posix()


def build_bundle_manifest(base_dir: Path) -> dict:
    static_dir = base_dir / DATA_STATIC_VERSION_DIR
    index_dir = base_dir / DATA_STATIC_VERSION_DIR
    asset_dir = base_dir / DATA_STATIC_ASSET_DIR

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
        "hextech_seed_health": validate_hextech_seed_health(base_dir),
        "synergy_data_files": synergy_data_files,
        "synergy_data_file": synergy_data_file,
        "source_files": source_files,
    }
    validate_bundle_manifest(manifest)
    return manifest
