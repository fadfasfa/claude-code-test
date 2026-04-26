from __future__ import annotations

"""开发与构建相关的清理工具。

这个模块只负责删除构建产物、运行时缓存和临时文件，不做业务数据变更。
与打包逻辑强相关的清理动作放在这里，方便在构建前后统一调用。
"""

import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = DATA_DIR / "runtime"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
AUDIT_DIR = DATA_DIR / "audit"
BUILD_DIR = BASE_DIR / "build"
DIST_DIR = BASE_DIR / "dist"

VOLATILE_OUTPUT_FILES = (
    RUNTIME_DIR / "Champion_Hextech_Cache.json",
    RUNTIME_DIR / "Champion_List_Cache.json",
    RUNTIME_DIR / "Champion_Synergy.json",
    RUNTIME_DIR / "scraper_status.json",
    RUNTIME_DIR / "startup_status.json",
    RUNTIME_DIR / "web_server_port.txt",
    RUNTIME_DIR / "browser_profile",
    RUNTIME_DIR / "hextech_system.log",
    RUNTIME_DIR / "hextech_runtime_summary.log",
    RUNTIME_DIR / "hextech_error.log",
    RUNTIME_DIR / "web_server_test.err.log",
    RUNTIME_DIR / "web_server_test.log",
    PROCESSED_DIR / "Champion_Hextech_Cache.json",
    PROCESSED_DIR / "Champion_List_Cache.json",
    RAW_DIR / "synergy" / "Champion_Synergy.json",
    CONFIG_DIR / "scraper_status.json",
    CONFIG_DIR / "startup_status.json",
    CONFIG_DIR / "web_server_port.txt",
    CONFIG_DIR / "hextech_system.log",
    CONFIG_DIR / "hextech_runtime_summary.log",
    CONFIG_DIR / "hextech_error.log",
    CONFIG_DIR / "web_server_test.err.log",
    CONFIG_DIR / "web_server_test.log",
)

VOLATILE_OUTPUT_GLOBS = (
    RUNTIME_DIR / "Hextech_Data_*.csv",
    RUNTIME_DIR / "*.log",
    AUDIT_DIR / "*.log",
    CONFIG_DIR / "*.log",
)


def cleanup_python_caches() -> tuple[int, int]:
    removed_dirs = 0
    removed_files = 0
    for cache_dir in BASE_DIR.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            removed_dirs += 1
    for pattern in ("*.pyc", "*.pyo"):
        for pyc_file in BASE_DIR.rglob(pattern):
            if pyc_file.is_file():
                try:
                    pyc_file.unlink()
                    removed_files += 1
                except OSError:
                    pass
    return removed_dirs, removed_files


def _remove_target(target: Path) -> bool:
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()
    return True


def cleanup_runtime_outputs() -> list[Path]:
    removed: list[Path] = []

    for target in VOLATILE_OUTPUT_FILES:
        if _remove_target(target):
            removed.append(target)

    for pattern in VOLATILE_OUTPUT_GLOBS:
        for target in pattern.parent.glob(pattern.name):
            if _remove_target(target):
                removed.append(target)

    return removed


def cleanup_build_outputs() -> list[Path]:
    removed: list[Path] = []
    for target in (BUILD_DIR, DIST_DIR, BASE_DIR / "version_info.txt"):
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
            removed.append(target)
    return removed

