"""安全归档旧 ``run/data``，且不读取浏览器 profile 内容。

普通文件记录 SHA-256；日志只记录汇总数量和字节数；
``runtime/profile`` 和 ``raw`` 下疑似敏感项只做不透明移动，不读取、不哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SENSITIVE_PART = re.compile(
    r"(?:^|[._-])(?:cookie|auth|session|credential|token|profile)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArchivePaths:
    source: Path
    destination: Path

    @property
    def archived_data(self) -> Path:
        return self.destination / "data"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _iter_files_without_sensitive(root: Path, opaque_raw: dict[str, int]) -> Iterable[Path]:
    """枚举可审计文件；绝不进入 profile 或疑似敏感的 raw 目录。"""

    profile = (root / "runtime" / "profile").resolve()
    for current, directories, filenames in os.walk(root):
        current_path = Path(current).resolve()
        if current_path == profile or _is_under(current_path, profile):
            directories[:] = []
            continue
        relative_parts = tuple(part.lower() for part in current_path.relative_to(root).parts)
        kept_directories: list[str] = []
        for name in directories:
            candidate = (current_path / name).resolve()
            candidate_parts = (*relative_parts, name.lower())
            if candidate == profile:
                continue
            if candidate_parts and candidate_parts[0] == "raw" and any(
                SENSITIVE_PART.search(part) for part in candidate_parts
            ):
                opaque_raw["directory_count"] += 1
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for filename in filenames:
            file_parts = (*relative_parts, filename.lower())
            if file_parts and file_parts[0] == "raw" and any(
                SENSITIVE_PART.search(part) for part in file_parts
            ):
                opaque_raw["file_count"] += 1
                continue
            yield current_path / filename


def _classification(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    parts = tuple(part.lower() for part in relative.parts)
    if len(parts) >= 2 and parts[:2] == ("runtime", "logs"):
        return "logs"
    if len(parts) >= 2 and parts[:2] == ("runtime", "cache"):
        return "runtime_cache"
    if parts and parts[0] in {"raw", "processed", "static"}:
        return parts[0]
    return "other"


def build_manifest(source: Path) -> dict[str, Any]:
    source = source.resolve()
    profile = source / "runtime" / "profile"
    hashed: list[dict[str, Any]] = []
    logs_count = 0
    logs_bytes = 0
    totals: dict[str, dict[str, int]] = {}
    opaque_raw = {"directory_count": 0, "file_count": 0}
    for path in _iter_files_without_sensitive(source, opaque_raw):
        kind = _classification(path, source)
        size = path.stat().st_size
        bucket = totals.setdefault(kind, {"file_count": 0, "total_bytes": 0})
        bucket["file_count"] += 1
        bucket["total_bytes"] += size
        if kind == "logs":
            logs_count += 1
            logs_bytes += size
            continue
        hashed.append(
            {
                "relative_path": path.relative_to(source).as_posix(),
                "size": size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": _sha256(path),
                "classification": kind,
            }
        )
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "run/data",
        "profile": {"opaque": True, "present": profile.is_dir()},
        "sensitive_raw": {
            "opaque": True,
            "present": bool(opaque_raw["directory_count"] or opaque_raw["file_count"]),
            **opaque_raw,
        },
        "logs": {"content_read": False, "file_count": logs_count, "total_bytes": logs_bytes},
        "totals": totals,
        "hashed_files": hashed,
    }


def _verify_manifest(root: Path, manifest: dict[str, Any]) -> None:
    rebuilt = build_manifest(root)
    for key in ("profile", "sensitive_raw", "logs", "totals", "hashed_files"):
        if rebuilt[key] != manifest[key]:
            raise RuntimeError(f"归档校验失败：{key}")


def archive_legacy_data(paths: ArchivePaths) -> Path:
    source = paths.source.resolve()
    destination = paths.destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    if source.anchor.lower() != destination.anchor.lower():
        raise RuntimeError("敏感 profile 只允许同卷原子移动")

    manifest = build_manifest(source)
    destination.mkdir(parents=True, exist_ok=False)
    archived_data = paths.archived_data
    archived_data.mkdir()
    moved: list[tuple[Path, Path]] = []
    try:
        for child in source.iterdir():
            target = archived_data / child.name
            os.replace(child, target)
            moved.append((target, child))
        _verify_manifest(archived_data, manifest)
        (destination / "archive_manifest.v1.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        source.rmdir()
        return destination
    except Exception:
        for archived, original in reversed(moved):
            if archived.exists() and not original.exists():
                os.replace(archived, original)
        (destination / "archive_manifest.v1.json").unlink(missing_ok=True)
        if archived_data.exists():
            archived_data.rmdir()
        if destination.exists():
            destination.rmdir()
        raise


def default_destination(repository_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return repository_root / ".archive" / f"hextech-data-v1-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全归档旧 run/data；浏览器 profile 始终不透明处理。")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--apply", action="store_true", help="执行归档；默认只构建并验证内存清单。")
    args = parser.parse_args(argv)
    root = args.repository_root.resolve()
    source = root / "run" / "data"
    destination = args.destination or default_destination(root)
    if args.apply:
        print(archive_legacy_data(ArchivePaths(source, destination)))
    else:
        manifest = build_manifest(source)
        print(json.dumps({key: value for key, value in manifest.items() if key != "hashed_files"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
