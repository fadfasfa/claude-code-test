from __future__ import annotations

"""开发与构建相关的清理工具。

这个模块只负责删除构建产物、运行时缓存和临时文件，不做业务数据变更。
与打包逻辑强相关的清理动作放在这里，方便在构建前后统一调用。
"""

import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DATA_DIR = BASE_DIR / "data" / "static"
INDEX_DATA_DIR = BASE_DIR / "data" / "indexes"
RUNTIME_RAW_DATA_DIR = BASE_DIR / "data" / "raw"
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
BUILD_DIR = BASE_DIR / "build"
DIST_DIR = BASE_DIR / "dist"

VOLATILE_CONFIG_FILES = (
    "Champion_Hextech_Cache.json",
    "Champion_List_Cache.json",
    "Champion_Synergy.json",
    "scraper_status.json",
    "startup_status.json",
    "web_server_port.txt",
    "hextech_system.log",
    "hextech_runtime_summary.log",
    "hextech_error.log",
    "web_server_test.err.log",
    "web_server_test.log",
)

VOLATILE_CONFIG_GLOBS = (
    "*.log",
)

VOLATILE_RAW_DATA_GLOBS = (
    "Hextech_Data_*.csv",
)

VOLATILE_RUNTIME_FILES = (
    RUNTIME_DIR / "state" / "scraper_status.json",
    RUNTIME_DIR / "state" / "startup_status.json",
    RUNTIME_DIR / "state" / "web_server_port.txt",
    RUNTIME_DIR / "cache" / "Champion_List_Cache.json",
    RUNTIME_DIR / "cache" / "Champion_Hextech_Cache.json",
    RUNTIME_DIR / "cache" / "Champion_Hextech_Cache",
    RUNTIME_DIR / "locks" / "heal_worker.lock",
    RUNTIME_DIR / "profile" / "browser_profile",
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


def cleanup_runtime_outputs() -> list[Path]:
    removed: list[Path] = []

    for name in VOLATILE_CONFIG_FILES:
        target = RUNTIME_DIR / name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
            removed.append(target)

    for pattern in VOLATILE_CONFIG_GLOBS:
        for target in RUNTIME_RAW_DATA_DIR.rglob(pattern):
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
                removed.append(target)

    for pattern in VOLATILE_RAW_DATA_GLOBS:
        for target in RUNTIME_RAW_DATA_DIR.glob(pattern):
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
                removed.append(target)

    for target in VOLATILE_RUNTIME_FILES:
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
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

