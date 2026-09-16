"""开发与构建相关的清理工具。

这个模块只处理 Python 生成物和构建产物。运行态由各 owner 与 retention
管理；工具不得恢复或清理已经退役的 ``run/data`` 布局。

调用方: build_package; 关键依赖: 见 imports。
"""

from __future__ import annotations

import shutil
import argparse
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
BUILD_DIR = BASE_DIR / "build"
DIST_DIR = BASE_DIR / "dist"
LEGACY_SPEC_FILE = BASE_DIR / "Hextech伴生终端.spec"
CANDIDATE_RELEASE_PATTERN = re.compile(r"HextechCompanion-\d{8}(?:T\d{6})?-rf[\w.-]*")
OBSOLETE_CANDIDATE_SHORTCUT_NAME = "Hextech重构候选.lnk"


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


def cleanup_build_outputs(*, dry_run: bool = False) -> list[Path]:
    removed: list[Path] = []
    for target in (BUILD_DIR, DIST_DIR, BASE_DIR / "version_info.txt", LEGACY_SPEC_FILE):
        _remove_path(target, dry_run=dry_run, removed=removed)
    return removed


def candidate_package_targets(base_dir: Path = BASE_DIR) -> list[Path]:
    """只枚举受管候选二进制，不包含 smoke 报告或隔离用户数据。"""

    artifacts = (base_dir / ".artifacts").resolve(strict=False)
    targets: list[Path] = []
    for root in (artifacts / "hx" / "releases", artifacts / "hx" / "staging"):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            name = path.stem if path.is_file() and path.suffix.casefold() == ".zip" else path.name
            if CANDIDATE_RELEASE_PATTERN.fullmatch(name):
                targets.append(path)
    for smoke_root in artifacts.glob("rf*-smoke"):
        if not smoke_root.is_dir():
            continue
        for path in smoke_root.iterdir():
            if path.is_dir() and CANDIDATE_RELEASE_PATTERN.fullmatch(path.name):
                targets.append(path)
    return sorted(set(targets), key=lambda path: str(path).casefold())


def cleanup_candidate_packages(*, base_dir: Path = BASE_DIR, dry_run: bool = False) -> list[Path]:
    artifacts = (base_dir / ".artifacts").resolve(strict=False)
    removed: list[Path] = []
    for target in candidate_package_targets(base_dir):
        resolved = target.resolve(strict=False)
        if artifacts not in resolved.parents or target.is_symlink():
            raise ValueError(f"拒绝清理越界或链接候选：{target}")
        _remove_path(target, dry_run=dry_run, removed=removed)
    return removed


def cleanup_obsolete_candidate_shortcut(path: Path, *, dry_run: bool = False) -> bool:
    """删除已核验的旧候选入口；稳定快捷方式名称永不匹配此函数。"""

    target = path.resolve(strict=False)
    if target.name != OBSOLETE_CANDIDATE_SHORTCUT_NAME or target.parent.name.casefold() != "desktop":
        raise ValueError(f"拒绝删除非候选桌面快捷方式：{target}")
    if target.exists() and (not target.is_file() or target.is_symlink()):
        raise ValueError(f"拒绝删除非普通候选快捷方式：{target}")
    return _remove_path(target, dry_run=dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清理 Hextech 构建产物、运行态临时文件和 Python 生成物。")
    parser.add_argument("--apply", action="store_true", help="实际删除；默认只 dry-run 输出清单。")
    parser.add_argument("--python-caches", action="store_true", help="扫描 __pycache__、.pyc、.pyo。")
    parser.add_argument("--build", action="store_true", help="扫描构建输出。")
    parser.add_argument("--candidate-packages", action="store_true", help="扫描受管 rf 候选、staging 与 smoke 包副本。")
    parser.add_argument("--candidate-shortcut", type=Path, help="精确删除旧 Hextech重构候选.lnk；不匹配时拒绝。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = not bool(args.apply)
    if not (args.python_caches or args.build or args.candidate_packages or args.candidate_shortcut):
        args.python_caches = True
    if args.python_caches:
        dirs, files = cleanup_python_caches(dry_run=dry_run)
        print(f"python_caches mode={'apply' if args.apply else 'dry-run'} dirs={dirs} files={files}")
    if args.build:
        removed = cleanup_build_outputs(dry_run=dry_run)
        print(f"build_outputs mode={'apply' if args.apply else 'dry-run'} count={len(removed)}")
        for path in removed:
            print(path)
    if args.candidate_packages:
        removed = cleanup_candidate_packages(dry_run=dry_run)
        print(f"candidate_packages mode={'apply' if args.apply else 'dry-run'} count={len(removed)}")
        for path in removed:
            print(path)
    if args.candidate_shortcut is not None:
        removed = cleanup_obsolete_candidate_shortcut(args.candidate_shortcut, dry_run=dry_run)
        print(f"candidate_shortcut mode={'apply' if args.apply else 'dry-run'} removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

