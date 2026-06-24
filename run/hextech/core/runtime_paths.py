"""运行态路径和资源定位入口。

路径实现目前集中在 `hextech.catalog.runtime_store`，本模块提供 core 层稳定入口，避免调用方
重新关心源码态、冻结态和运行态目录的具体落点。
"""

from __future__ import annotations

from hextech.catalog.runtime_store import (
    build_runtime_cache_path,
    build_runtime_debug_path,
    build_runtime_lock_path,
    build_runtime_persisted_path,
    build_runtime_profile_path,
    build_runtime_state_path,
    ensure_private_runtime_dir,
    ensure_runtime_profile_dir,
    ensure_runtime_state_dir,
    get_runtime_cache_dir,
    get_runtime_data_dir,
    get_runtime_debug_dir,
    get_runtime_lock_dir,
    get_runtime_persisted_dir,
    get_runtime_profile_dir,
    get_runtime_root_dir,
    get_runtime_state_dir,
    resolve_runtime_file,
    runtime_priority_paths,
)


__all__ = [
    "build_runtime_cache_path",
    "build_runtime_debug_path",
    "build_runtime_lock_path",
    "build_runtime_persisted_path",
    "build_runtime_profile_path",
    "build_runtime_state_path",
    "ensure_private_runtime_dir",
    "ensure_runtime_profile_dir",
    "ensure_runtime_state_dir",
    "get_runtime_cache_dir",
    "get_runtime_data_dir",
    "get_runtime_debug_dir",
    "get_runtime_lock_dir",
    "get_runtime_persisted_dir",
    "get_runtime_profile_dir",
    "get_runtime_root_dir",
    "get_runtime_state_dir",
    "resolve_runtime_file",
    "runtime_priority_paths",
]
