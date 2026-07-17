"""开发与构建相关的清理工具。

这个模块只负责删除构建产物、运行时缓存和临时文件，不做业务数据变更。
与打包逻辑强相关的清理动作放在这里，方便在构建前后统一调用。

调用方: build_package; 关键依赖: 见 imports。
"""

from __future__ import annotations

import shutil
import argparse
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
STATIC_DATA_DIR = BASE_DIR / "data" / "static" / "version"
INDEX_DATA_DIR = BASE_DIR / "data" / "static" / "version"
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
RUNTIME_RAW_DATA_DIR = RUNTIME_DIR / "raw"
BUILD_DIR = BASE_DIR / "build"
DIST_DIR = BASE_DIR / "dist"
LEGACY_SPEC_FILE = BASE_DIR / "Hextech伴生终端.spec"

VOLATILE_CONFIG_FILES = (
    "Champion_Hextech_Cache.json",
    "Champion_List_Cache.json",
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


def _remove_path(target: Path, *, dry_run: bool, removed: list[Path] | None = None) -> bool:
    if not target.exists():
        return False
    if removed is not None:
        removed.append(target)
    if dry_run:
        return True
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()
    return True


def cleanup_python_caches(*, dry_run: bool = False) -> tuple[int, int]:
    removed_dirs = 0
    removed_files = 0
    for cache_dir in BASE_DIR.rglob("__pycache__"):
        if cache_dir.is_dir():
            if not dry_run:
                shutil.rmtree(cache_dir, ignore_errors=True)
            removed_dirs += 1
    for pattern in ("*.pyc", "*.pyo"):
        for pyc_file in BASE_DIR.rglob(pattern):
            if pyc_file.is_file():
                try:
                    if not dry_run:
                        pyc_file.unlink()
                    removed_files += 1
                except OSError:
                    pass
    return removed_dirs, removed_files


def cleanup_runtime_outputs(*, dry_run: bool = False) -> list[Path]:
    removed: list[Path] = []

    for name in VOLATILE_CONFIG_FILES:
        target = RUNTIME_DIR / name
        _remove_path(target, dry_run=dry_run, removed=removed)

    for pattern in VOLATILE_CONFIG_GLOBS:
        for target in RUNTIME_RAW_DATA_DIR.rglob(pattern):
            _remove_path(target, dry_run=dry_run, removed=removed)

    for pattern in VOLATILE_RAW_DATA_GLOBS:
        for target in RUNTIME_RAW_DATA_DIR.glob(pattern):
            _remove_path(target, dry_run=dry_run, removed=removed)

    for target in VOLATILE_RUNTIME_FILES:
        _remove_path(target, dry_run=dry_run, removed=removed)

    return removed


def cleanup_build_outputs(*, dry_run: bool = False) -> list[Path]:
    removed: list[Path] = []
    for target in (BUILD_DIR, DIST_DIR, BASE_DIR / "version_info.txt", LEGACY_SPEC_FILE):
        _remove_path(target, dry_run=dry_run, removed=removed)
    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清理 Hextech 构建产物、运行态临时文件和 Python 生成物。")
    parser.add_argument("--apply", action="store_true", help="实际删除；默认只 dry-run 输出清单。")
    parser.add_argument("--python-caches", action="store_true", help="扫描 __pycache__、.pyc、.pyo。")
    parser.add_argument("--runtime", action="store_true", help="扫描运行态临时输出。")
    parser.add_argument("--build", action="store_true", help="扫描构建输出。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = not bool(args.apply)
    if not (args.python_caches or args.runtime or args.build):
        args.python_caches = True
    if args.python_caches:
        dirs, files = cleanup_python_caches(dry_run=dry_run)
        print(f"python_caches mode={'apply' if args.apply else 'dry-run'} dirs={dirs} files={files}")
    if args.runtime:
        removed = cleanup_runtime_outputs(dry_run=dry_run)
        print(f"runtime_outputs mode={'apply' if args.apply else 'dry-run'} count={len(removed)}")
        for path in removed:
            print(path)
    if args.build:
        removed = cleanup_build_outputs(dry_run=dry_run)
        print(f"build_outputs mode={'apply' if args.apply else 'dry-run'} count={len(removed)}")
        for path in removed:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

