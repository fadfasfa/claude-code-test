"""开发与构建相关的清理工具。

这个模块只处理 Python 生成物和构建产物。运行态由各 owner 与 retention
管理；工具不得恢复或清理已经退役的 ``run/data`` 布局。

调用方: build_package; 关键依赖: 见 imports。
"""

from __future__ import annotations

import shutil
import argparse
import re
import stat
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
BUILD_DIR = BASE_DIR / "build"
DIST_DIR = BASE_DIR / "dist"
LEGACY_SPEC_FILE = BASE_DIR / "Hextech伴生终端.spec"
CANDIDATE_RELEASE_PATTERN = re.compile(r"HextechCompanion-\d{8}(?:T\d{6})?-rf[\w.-]*")
OBSOLETE_CANDIDATE_SHORTCUT_NAME = "Hextech重构候选.lnk"


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _reject_reparse_chain(path: Path, *, stop_at: Path | None = None) -> None:
    stop = Path(stop_at).absolute() if stop_at is not None else None
    raw = Path(path).absolute()
    for component in (raw, *raw.parents):
        if _is_reparse_point(component):
            raise ValueError(f"拒绝处理 reparse point：{component}")
        if stop is not None and component == stop:
            break


def _remove_path(target: Path, *, dry_run: bool, removed: list[Path] | None = None) -> bool:
    if not target.exists() and not target.is_symlink():
        return False
    if _is_reparse_point(target):
        raise RuntimeError(f"拒绝删除 reparse point：{target}")
    if removed is not None:
        removed.append(target)
    if dry_run:
        return True
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise RuntimeError(f"清理失败：{target}: {exc}") from exc
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"清理后目标仍存在：{target}")
    return True


def cleanup_python_caches(*, dry_run: bool = False) -> tuple[int, int]:
    removed_dirs = 0
    removed_files = 0
    cache_dirs: list[Path] = []
    cache_files: list[Path] = []
    stack = [BASE_DIR]
    while stack:
        current = stack.pop()
        if _is_reparse_point(current):
            continue
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            raise RuntimeError(f"扫描 Python 缓存失败：{current}: {exc}") from exc
        for entry in entries:
            if _is_reparse_point(entry):
                continue
            if entry.is_dir():
                if entry.name == "__pycache__":
                    cache_dirs.append(entry)
                    try:
                        cache_entries = list(entry.iterdir())
                    except OSError as exc:
                        raise RuntimeError(f"扫描 Python 缓存失败：{entry}: {exc}") from exc
                    for cache_entry in cache_entries:
                        if (
                            not _is_reparse_point(cache_entry)
                            and cache_entry.is_file()
                            and cache_entry.suffix.casefold() in {".pyc", ".pyo"}
                        ):
                            cache_files.append(cache_entry)
                else:
                    stack.append(entry)
            elif entry.is_file() and entry.suffix.casefold() in {".pyc", ".pyo"}:
                cache_files.append(entry)
    for pyc_file in cache_files:
        if _remove_path(pyc_file, dry_run=dry_run):
            removed_files += 1
    for cache_dir in sorted(cache_dirs, key=lambda path: len(path.parts), reverse=True):
        if _remove_path(cache_dir, dry_run=dry_run):
            removed_dirs += 1
    return removed_dirs, removed_files


def cleanup_build_outputs(*, dry_run: bool = False) -> list[Path]:
    removed: list[Path] = []
    for target in (BUILD_DIR, DIST_DIR, BASE_DIR / "version_info.txt", LEGACY_SPEC_FILE):
        _remove_path(target, dry_run=dry_run, removed=removed)
    return removed


def candidate_package_targets(base_dir: Path = BASE_DIR) -> list[Path]:
    """只枚举受管候选二进制，不包含 smoke 报告或隔离用户数据。"""

    artifacts = base_dir / ".artifacts"
    _reject_reparse_chain(artifacts, stop_at=base_dir)
    targets: list[Path] = []
    for root in (artifacts / "hx" / "releases", artifacts / "hx" / "staging"):
        _reject_reparse_chain(root, stop_at=artifacts)
        if not root.is_dir():
            continue
        for path in root.iterdir():
            _reject_reparse_chain(path, stop_at=root)
            name = path.stem if path.is_file() and path.suffix.casefold() == ".zip" else path.name
            if CANDIDATE_RELEASE_PATTERN.fullmatch(name):
                targets.append(path)
    for smoke_root in artifacts.glob("rf*-smoke"):
        _reject_reparse_chain(smoke_root, stop_at=artifacts)
        if not smoke_root.is_dir():
            continue
        for path in smoke_root.iterdir():
            _reject_reparse_chain(path, stop_at=smoke_root)
            if path.is_dir() and CANDIDATE_RELEASE_PATTERN.fullmatch(path.name):
                targets.append(path)
    return sorted(set(targets), key=lambda path: str(path).casefold())


def cleanup_candidate_packages(*, base_dir: Path = BASE_DIR, dry_run: bool = False) -> list[Path]:
    artifacts = base_dir / ".artifacts"
    _reject_reparse_chain(artifacts, stop_at=base_dir)
    removed: list[Path] = []
    for target in candidate_package_targets(base_dir):
        _reject_reparse_chain(target, stop_at=artifacts)
        resolved = target.resolve(strict=False)
        if artifacts not in resolved.parents:
            raise ValueError(f"拒绝清理越界或链接候选：{target}")
        _remove_path(target, dry_run=dry_run, removed=removed)
    return removed


def cleanup_obsolete_candidate_shortcut(path: Path, *, dry_run: bool = False) -> bool:
    """删除已核验的旧候选入口；稳定快捷方式名称永不匹配此函数。"""

    raw = Path(path).expanduser()
    if raw.name != OBSOLETE_CANDIDATE_SHORTCUT_NAME or raw.parent.name.casefold() != "desktop":
        raise ValueError(f"拒绝删除非候选桌面快捷方式：{raw}")
    for component in (raw, *raw.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"无法核验候选快捷方式路径：{component}") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"拒绝处理 reparse point 快捷方式：{component}")
    target = raw.resolve(strict=False)
    if target.exists() and not target.is_file():
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

