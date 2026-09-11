"""部署时区分安装凭据、真实刷新周期与合法固定代，不改写运行态。"""
from __future__ import annotations
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
import psutil

from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
from hextech.modules.data.generation.validation import SnapshotValidationError


_CHECKPOINT_SOURCES = {"catalog", "aramkit", "blitz", "apex", "mayhem"}
_CHECKPOINT_PHASES = {"catalog", "core", "optional", "complete"}


def _aware_iso(value: object) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _checkpoint_source_errors(
    completed: Mapping[str, Any],
    *,
    checkpoint_catalog_id: str,
) -> list[str]:
    if not set(completed).issubset(_CHECKPOINT_SOURCES):
        return ["refresh checkpoint completed_sources 包含未知来源"]
    for source, pointer in completed.items():
        if not isinstance(pointer, Mapping):
            return [f"refresh checkpoint completed source 不是 pointer：{source}"]
        if source == "catalog":
            if not str(pointer.get("catalog_generation_id") or ""):
                return ["refresh checkpoint completed Catalog 缺少 identity"]
            continue
        if (
            not str(pointer.get("run_id") or "")
            or str(pointer.get("catalog_generation_id") or "") != checkpoint_catalog_id
        ):
            return [f"refresh checkpoint completed source identity 无效：{source}"]
    return []


def refresh_checkpoint_errors(
    checkpoint: Any,
    catalog_id: str,
    *,
    current_generation_id: str = "",
) -> list[str]:
    if not isinstance(checkpoint, dict):
        return ["refresh checkpoint 不是对象"]
    state = str(checkpoint.get("state") or "")
    pending = checkpoint.get("pending_sources")
    completed, failures = checkpoint.get("completed_sources", {}), checkpoint.get("failures", {})
    catalog = checkpoint.get("catalog", {})
    checkpoint_catalog_id = (
        str(catalog.get("catalog_generation_id") or "")
        if isinstance(catalog, Mapping)
        else ""
    )
    if (
        type(checkpoint.get("schema_version")) is not int
        or checkpoint.get("schema_version") != 1
        or not str(checkpoint.get("cycle_id") or "")
        or not _aware_iso(checkpoint.get("created_at"))
        or not _aware_iso(checkpoint.get("updated_at"))
        or str(checkpoint.get("scope") or "") not in {"due", "core"}
        or str(checkpoint.get("refresh_phase") or "") not in _CHECKPOINT_PHASES
        or not isinstance(pending, list)
        or not all(isinstance(item, str) and item in _CHECKPOINT_SOURCES for item in pending)
        or len(set(pending)) != len(pending)
        or not isinstance(completed, Mapping)
        or not isinstance(failures, Mapping)
        or not isinstance(catalog, Mapping)
        or not checkpoint_catalog_id
        or not isinstance(checkpoint.get("core_generation_id"), str)
    ):
        return ["active refresh checkpoint 周期、来源或 Catalog 不一致"]
    source_errors = _checkpoint_source_errors(
        completed,
        checkpoint_catalog_id=checkpoint_catalog_id,
    )
    if source_errors:
        return source_errors
    if set(pending) & set(completed) or not set(failures).issubset(set(pending)):
        return ["active refresh checkpoint 周期、来源或 Catalog 不一致"]
    if state == "complete":
        if (
            str(checkpoint.get("refresh_phase") or "") != "complete"
            or pending
            or failures
        ):
            return ["refresh checkpoint complete 仍有 pending/failures"]
        # complete 是其自身历史周期的证据，不要求旧 core/Catalog 等于当前代。
        return []
    if (
        state != "in_progress"
        or str(checkpoint.get("refresh_phase") or "") == "complete"
        or checkpoint_catalog_id != catalog_id
        or (
            str(checkpoint.get("core_generation_id") or "")
            and current_generation_id
            and str(checkpoint.get("core_generation_id")) != current_generation_id
        )
    ):
        return ["active refresh checkpoint 周期、来源或 Catalog 不一致"]
    # 仍在进行或失败的抓取不能成为本次部署完成证明；原文件原样保留。
    return [f"active refresh checkpoint 尚未完成：state={state}"]


def validated_launch_generation(root: Path, expected: Mapping[str, Any], *, build_id: str,
                                launch_started_at: float, desktop_pid: int | None,
                                current_generation_id: str) -> tuple[str, list[str]]:
    if launch_started_at <= 0:
        return "", []  # 无launch身份的只读单元检查，不授予额外合法代。
    try:
        p = root / "state/data-service/cohort_selection.v1.json"
        selection = json.loads(p.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(selection.get("updated_at") or "").replace("Z", "+00:00"))
        if updated.tzinfo is None:
            raise ValueError("selection timezone missing")
        import time
        started = float(selection.get("writer_pid_started_at") or 0)
        if (selection.get("schema_version") != 2 or selection.get("writer_role") != "desktop_seed"
            or selection.get("writer_build_id") != build_id
            or selection.get("bundle_generation_id") != expected.get("generation_id")
            or not desktop_pid or selection.get("writer_pid") != desktop_pid
            or not math.isfinite(started) or started < launch_started_at - 2
            or abs(psutil.Process(desktop_pid).create_time() - started) > 2
            or not launch_started_at <= updated.timestamp() <= time.time() + 1):
            raise ValueError("launch selection ownership/time mismatch")
        fingerprint = str(expected.get("_source_fingerprint") or "")
        if fingerprint and selection.get("writer_source_fingerprint") != fingerprint:
            raise ValueError("launch source fingerprint mismatch")
        identity = str(selection.get("selected_generation_id") or "")
        candidate = validate_generation_cohort(root, identity)
        current = validate_generation_cohort(root, current_generation_id)
        baseline = validate_generation_cohort(root, str(expected.get("generation_id") or ""))
        if not baseline.sort_time <= candidate.sort_time <= current.sort_time:
            raise ValueError("launch generation order mismatch")
        if (candidate.production_pool_id != current.production_pool_id
            or candidate.production_pool_count != current.production_pool_count
            or candidate.pointers["catalog"]["catalog_generation_id"] != current.pointers["catalog"]["catalog_generation_id"]):
            raise ValueError("launch pool/catalog mismatch")
        return identity, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SnapshotValidationError, psutil.Error) as exc:
        return "", [f"launch cohort 未通过完整验证：{type(exc).__name__}: {exc}"]


def validated_game_stats_generation(root: Path, visibility: Mapping[str, Any], report: Mapping[str, Any],
                                    current_id: str) -> tuple[str, list[str]]:
    identity = str(visibility.get("stats_generation_id") or "")
    if not identity or identity == current_id:
        return current_id, []
    try:
        host, window, scope = visibility.get("host", {}), visibility.get("window", {}), report.get("stats_scope", {})
        game = str(window.get("game_instance_id") or "")
        if (host.get("gameflow") is not True or not game or report.get("session_id") != game
            or window.get("desync") is not False or window.get("identity_desync") is not False
            or scope.get("frozen") is not True or scope.get("stats_generation_id") != identity
            or scope.get("stage_context", {}).get("game_instance_id") != game
            or report.get("stats_generation_id") != identity):
            raise ValueError("stats pin lacks active game/selection proof")
        pinned = validate_generation_cohort(root, identity)
        current = validate_generation_cohort(root, current_id)
        if (pinned.sort_time > current.sort_time or pinned.production_pool_id != current.production_pool_id
            or pinned.production_pool_count != current.production_pool_count
            or pinned.pointers["catalog"]["catalog_generation_id"] != current.pointers["catalog"]["catalog_generation_id"]):
            raise ValueError("stats pin cohort mismatch")
        return identity, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SnapshotValidationError) as exc:
        return current_id, [f"stats pin 未通过完整验证：{type(exc).__name__}"]
