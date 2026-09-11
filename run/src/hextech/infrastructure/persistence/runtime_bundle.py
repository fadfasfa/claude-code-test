"""把包内完整 seed generation 原子播种到运行态 snapshots。"""

from __future__ import annotations

import json
import hashlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.catalog.runtime_store import build_runtime_state_path, ensure_private_runtime_dir
BUNDLE_MANIFEST_NAME = "bundle_manifest.json"


SEED_PREFIX = PurePosixPath("resources/seeds")
logger = logging.getLogger(__name__)


def _empty_manifest() -> dict:
    return {
        "catalog_files": [],
        "asset_files": [],
        "seed_files": [],
        "seed_sha256": {},
        "cohort_seed": {},
        "cohort_seed_files": [],
        "cohort_seed_sha256": {},
        "source_files": [],
    }


def _write_bundle_manifest_startup_warning(status: str, warning: str, manifest_path: Path) -> None:
    status_path = Path(build_runtime_state_path("startup_status.json"))
    ensure_private_runtime_dir(status_path.parent)
    payload: dict = {}
    if status_path.exists():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    payload["bundle_manifest"] = {
        "status": status,
        "warning": warning,
        "path": manifest_path.name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if warning and not str(payload.get("last_error") or "").strip():
        payload["last_error"] = warning
    atomic_write_json(status_path, payload)


def _write_verified_snapshot_startup_status(snapshot_dir: Path, *, cohort_state: str = "") -> None:
    from hextech.modules.data.generation import DataSnapshotClient

    view = DataSnapshotClient(snapshot_dir).open_view()
    status = view.status()
    manifest = view.manifest
    # 安装凭据来自 cohort_selection/validation_receipt；不改写真实抓取周期。
    status_path = Path(build_runtime_state_path("startup_status.json"))
    ensure_private_runtime_dir(status_path.parent)
    payload: dict = {}
    if status_path.exists():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    selection: dict = {}
    selection_path = snapshot_dir.parent / "state" / "data-service" / "cohort_selection.v1.json"
    if selection_path.is_file():
        try:
            loaded_selection = json.loads(selection_path.read_text(encoding="utf-8"))
            if isinstance(loaded_selection, dict):
                selection = loaded_selection
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    payload.update(
        {
            "first_run": False,
            "hero_ready": manifest.champion_count > 0,
            "hextech_ready": manifest.stat_record_count > 0,
            "synergy_ready": True,
            "in_progress_tasks": [],
            "last_error": "",
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_snapshot": {
                **status,
                "source": "runtime_restored" if cohort_state == "runtime_restored" else "verified_bundle_seed",
                "cohort_install_state": cohort_state,
                "selected_source": str(selection.get("selected_source") or ""),
                "selected_generation_id": str(selection.get("selected_generation_id") or manifest.generation_id),
                "bundle_generation_id": str(selection.get("bundle_generation_id") or ""),
                "champion_count": manifest.champion_count,
                "augment_count": manifest.augment_count,
                "stat_record_count": manifest.stat_record_count,
            },
        }
    )
    atomic_write_json(status_path, payload, ensure_ascii=False, indent=2)


def _load_bundle_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / BUNDLE_MANIFEST_NAME
    if not manifest_path.is_file():
        logger.warning("bundle manifest 缺失：%s", manifest_path)
        _write_bundle_manifest_startup_warning("missing", "bundle_manifest_missing", manifest_path)
        return _empty_manifest()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest must be an object")
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("bundle manifest 无法读取：path=%s error=%s", manifest_path, type(exc).__name__)
        _write_bundle_manifest_startup_warning("error", "bundle_manifest_invalid", manifest_path)
        return _empty_manifest()


def _safe_seed_path(value: object) -> PurePosixPath | None:
    text = str(value).replace("\\", "/").strip()
    if not text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or SEED_PREFIX not in path.parents:
        return None
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seed_bundled_resources(*, bundle_root: str | Path, runtime_snapshot_dir: str | Path) -> bool:
    """优先安装完整 cohort；旧包仅在没有 current 时播种 generation。"""

    bundle_base = Path(bundle_root)
    snapshot_dir = Path(runtime_snapshot_dir)
    if not bundle_base.is_dir():
        return False
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort

    cohort_state = install_bundled_cohort(
        bundle_root=bundle_base,
        runtime_root=snapshot_dir.parent,
    )
    if cohort_state in {"installed", "already_current", "runtime_restored"}:
        _write_verified_snapshot_startup_status(snapshot_dir, cohort_state=cohort_state)
        return cohort_state == "installed"
    if cohort_state == "runtime_newer":
        return False
    if (snapshot_dir / "current.v2.json").exists():
        return False
    manifest = _load_bundle_manifest(bundle_base)
    hashes = manifest.get("seed_sha256")
    if not isinstance(hashes, dict):
        logger.warning("bundle seed 缺少摘要表")
        return False

    files: list[tuple[PurePosixPath, Path, Path]] = []
    for raw_path in manifest.get("seed_files", []):
        seed_path = _safe_seed_path(raw_path)
        if seed_path is None:
            logger.warning("bundle seed 路径无效：%s", raw_path)
            return False
        source = bundle_base.joinpath(*seed_path.parts)
        expected = str(hashes.get(seed_path.as_posix()) or "").lower()
        if len(expected) != 64 or not source.is_file() or _sha256(source) != expected:
            logger.warning("bundle seed 摘要不匹配：%s", source)
            return False
        relative = seed_path.relative_to(SEED_PREFIX)
        files.append((seed_path, source, snapshot_dir.joinpath(*relative.parts)))
    if not files or not any(path.name == "current.v2.json" for path, _, _ in files):
        return False

    copied: list[Path] = []
    try:
        for seed_path, source, target in sorted(files, key=lambda item: item[0].name == "current.v2.json"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target)
        _write_verified_snapshot_startup_status(snapshot_dir)
        return True
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        raise


__all__ = ["seed_bundled_resources"]
