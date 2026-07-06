"""Hextech 运行态数据迁移工具。

用途：
- 生成 `run/data` 的只读 inventory、备份清单和迁移 manifest。
- 将旧 `data/raw/**` 与 `data/processed/**` 收口到 `data/runtime/raw|cache/processed`，
  并把可用首启 CSV 种子复制到 `data/seed/startup/hextech`。
- 排除凭据/登录态文件；只记录路径和原因，不读取内容。

默认 dry-run 不改文件。执行迁移必须显式传入 `--apply`。

调用方: tests.test_runtime_data_migration; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


RUN_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = RUN_DIR.parent / ".artifacts" / "hextech" / "data-migration"
HEXTECH_SNAPSHOT_FILENAME_RE = re.compile(r"^Hextech_Data_\d{4}-\d{2}-\d{2}\.csv$")
SENSITIVE_NAME_PARTS = (
    "api_key",
    "auth",
    "authorization",
    "token",
    "cookie",
    "credential",
    "lcu",
    "nonce",
    "riot",
    "session",
    "secret",
    "password",
    "local.yaml",
    "proxies.json",
    "accounts.json",
)
MIGRATION_HINT_FILENAME = "MIGRATED_TO_RUNTIME.txt"


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    target: Path
    category: str
    action: str


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def is_sensitive_path(path: Path) -> bool:
    text = path.as_posix().lower()
    return any(part in text for part in SENSITIVE_NAME_PARTS)


def should_skip_migration_file(path: Path) -> bool:
    return path.name == MIGRATION_HINT_FILENAME or is_sensitive_path(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, *, root: Path, category: str, include_hash: bool) -> dict[str, Any]:
    stat = path.stat()
    record: dict[str, Any] = {
        "path": _relative(path, root),
        "category": category,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    if include_hash:
        record["sha256"] = _sha256_file(path)
    return record


def _directory_summary(path: Path, *, root: Path) -> dict[str, Any]:
    total_files = 0
    total_bytes = 0
    latest_mtime = 0.0
    sensitive: list[dict[str, str]] = []
    if path.exists():
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            if is_sensitive_path(child):
                sensitive.append({"path": _relative(child, root), "reason": "sensitive_name"})
                continue
            total_files += 1
            try:
                stat = child.stat()
            except OSError:
                continue
            total_bytes += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
    return {
        "path": _relative(path, root),
        "exists": path.exists(),
        "file_count": total_files,
        "bytes": total_bytes,
        "latest_mtime": latest_mtime,
        "sensitive_excluded": sensitive,
    }


def build_inventory(base_dir: Path = RUN_DIR) -> dict[str, Any]:
    data_dir = base_dir / "data"
    return {
        "schema_version": 1,
        "generated_at": _utc_stamp(),
        "base_dir": str(base_dir),
        "directories": {
            "raw": _directory_summary(data_dir / "raw", root=base_dir),
            "processed": _directory_summary(data_dir / "processed", root=base_dir),
            "runtime": _directory_summary(data_dir / "runtime", root=base_dir),
            "seed_startup_snapshot": _directory_summary(data_dir / "seed" / "startup", root=base_dir),
        },
    }


def _iter_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return sorted(child for child in path.rglob("*") if child.is_file())


def build_migration_plans(base_dir: Path = RUN_DIR) -> list[MigrationPlan]:
    data_dir = base_dir / "data"
    plans: list[MigrationPlan] = []
    snapshot_dir = data_dir / "seed" / "startup" / "hextech"
    runtime_hextech = data_dir / "runtime" / "raw" / "hextech"
    legacy_hextech = data_dir / "raw" / "hextech"
    for source_dir in (runtime_hextech, legacy_hextech):
        for source in _iter_files(source_dir):
            if HEXTECH_SNAPSHOT_FILENAME_RE.match(source.name) and not should_skip_migration_file(source):
                target = snapshot_dir / source.name
                plans.append(MigrationPlan(source=source, target=target, category="startup_snapshot/hextech", action="copy"))

    for subdir in ("hextech", "synergy"):
        source_dir = data_dir / "raw" / subdir
        target_dir = data_dir / "runtime" / "raw" / subdir
        for source in _iter_files(source_dir):
            if should_skip_migration_file(source):
                continue
            plans.append(
                MigrationPlan(
                    source=source,
                    target=target_dir / source.relative_to(source_dir),
                    category=f"raw/{subdir}",
                    action="move",
                )
            )

    processed_dir = data_dir / "processed"
    processed_target = data_dir / "runtime" / "cache" / "processed"
    for source in _iter_files(processed_dir):
        if should_skip_migration_file(source):
            continue
        plans.append(
            MigrationPlan(
                source=source,
                target=processed_target / source.relative_to(processed_dir),
                category="cache/processed",
                action="move",
                )
            )
    return plans


def _backup_target(source: Path, *, base_dir: Path, backup_dir: Path) -> Path:
    return backup_dir / _relative(source, base_dir)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def execute_migration(
    *,
    base_dir: Path = RUN_DIR,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    apply: bool = False,
) -> dict[str, Any]:
    run_id = _utc_stamp()
    output_dir = artifact_root / run_id
    backup_dir = output_dir / "backup"
    inventory = build_inventory(base_dir)
    plans = build_migration_plans(base_dir)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "mode": "apply" if apply else "dry-run",
        "base_dir": str(base_dir),
        "output_dir": str(output_dir),
        "inventory": inventory,
        "entries": [],
        "sensitive_excluded": [],
    }

    for root_name, summary in inventory["directories"].items():
        for item in summary.get("sensitive_excluded", []):
            manifest["sensitive_excluded"].append({"root": root_name, **item})

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "inventory.v1.json", inventory)

    for plan in plans:
        if not plan.source.exists():
            continue
        if should_skip_migration_file(plan.source):
            manifest["sensitive_excluded"].append({"path": _relative(plan.source, base_dir), "reason": "sensitive_name"})
            continue

        source_record = _file_record(plan.source, root=base_dir, category=plan.category, include_hash=True)
        entry = {
            "source": _relative(plan.source, base_dir),
            "target": _relative(plan.target, base_dir),
            "action": plan.action,
            "category": plan.category,
            "size": source_record["size"],
            "mtime": source_record["mtime"],
            "sha256": source_record["sha256"],
            "backup": _relative(_backup_target(plan.source, base_dir=base_dir, backup_dir=backup_dir), output_dir),
            "rollback_command": f"move {plan.target} {plan.source}" if plan.action == "move" else f"remove copied seed {plan.target}",
            "applied": False,
        }
        manifest["entries"].append(entry)

        if not apply:
            continue

        backup_target = _backup_target(plan.source, base_dir=base_dir, backup_dir=backup_dir)
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.source, backup_target)
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        if plan.action == "move":
            if plan.target.exists():
                raise FileExistsError(f"迁移目标已存在，停止避免覆盖：{plan.target}")
            shutil.move(str(plan.source), str(plan.target))
        elif plan.action == "copy":
            if not plan.target.exists():
                shutil.copy2(plan.source, plan.target)
        entry["applied"] = True

    if apply:
        for relative_dir in ("data/raw/hextech", "data/raw/synergy", "data/processed"):
            hint_dir = base_dir / relative_dir
            if hint_dir.exists():
                hint_dir.mkdir(parents=True, exist_ok=True)
                hint = hint_dir / "MIGRATED_TO_RUNTIME.txt"
                if not hint.exists():
                    hint.write_text(
                        "本目录已迁移到 data/runtime/raw 或 data/runtime/cache/processed；旧路径仅保留兼容读取窗口。\n",
                        encoding="utf-8",
                    )

    _write_json(output_dir / "migration_manifest.v1.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成或执行 Hextech runtime 数据迁移清单。")
    parser.add_argument("--apply", action="store_true", help="执行迁移；默认只生成 dry-run 清单。")
    parser.add_argument("--base-dir", default=str(RUN_DIR), help="run 目录；测试时可指向临时 fixture。")
    parser.add_argument(
        "--artifact-root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help="迁移清单和备份输出目录；默认写入 .artifacts/hextech/data-migration。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = execute_migration(
        base_dir=Path(args.base_dir),
        artifact_root=Path(args.artifact_root),
        apply=bool(args.apply),
    )
    print(f"migration_manifest={Path(manifest['output_dir']) / 'migration_manifest.v1.json'}")
    print(f"mode={manifest['mode']} entries={len(manifest['entries'])} sensitive_excluded={len(manifest['sensitive_excluded'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
