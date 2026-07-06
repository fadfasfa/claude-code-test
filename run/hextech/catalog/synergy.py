"""协同数据定位入口。

协同 snapshot 的运行态路径仍由 `runtime_store` 统一管理；本模块提供 catalog 层协同域入口。

调用方: 见 import 此模块的代码; 关键依赖: catalog.runtime_store。
"""

from __future__ import annotations

from hextech.catalog.runtime_store import (
    build_next_synergy_snapshot_path,
    build_synergy_data_path,
    build_synergy_latest_pointer_path,
    build_synergy_legacy_data_path,
    build_synergy_refresh_status_path,
    build_synergy_snapshot_path,
    get_latest_synergy_snapshot_path,
    get_runtime_synergy_data_dir,
    is_synergy_snapshot_filename,
    iter_synergy_snapshot_files,
    load_synergy_latest_pointer,
    load_synergy_refresh_status,
    resolve_synergy_snapshot_from_pointer,
)


__all__ = [
    "build_next_synergy_snapshot_path",
    "build_synergy_data_path",
    "build_synergy_latest_pointer_path",
    "build_synergy_legacy_data_path",
    "build_synergy_refresh_status_path",
    "build_synergy_snapshot_path",
    "get_latest_synergy_snapshot_path",
    "get_runtime_synergy_data_dir",
    "is_synergy_snapshot_filename",
    "iter_synergy_snapshot_files",
    "load_synergy_latest_pointer",
    "load_synergy_refresh_status",
    "resolve_synergy_snapshot_from_pointer",
]
