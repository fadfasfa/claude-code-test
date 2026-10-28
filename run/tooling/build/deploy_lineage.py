"""Deployment lineage uses only fully validated immutable v4 releases."""
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import closure
from hextech.modules.data.generation.validation import SnapshotValidationError


def _candidate(root: Path, generation_id: str):
    view = DataSnapshotClient(root / "snapshots").open_generation(generation_id)
    catalog, *_ = closure(root, view.manifest)
    pool = view.get_overlay_hints()["source"]["production_augment_pool"]
    return view.manifest, pool, catalog.generation_id


def validated_launch_generation(root: Path, expected: Mapping[str, Any], *, build_id: str,
                                launch_started_at: float, desktop_pid: int | None,
                                current_generation_id: str) -> tuple[str, list[str]]:
    # App startup consumes one validated release head; no second cohort selection exists.
    try:
        _candidate(root, current_generation_id)
        _candidate(Path(str(expected.get("_seed_runtime") or root)), str(expected["generation_id"]))
        return "", []
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return "", [f"launch release invalid: {type(exc).__name__}: {exc}"]


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
        pinned = _candidate(root, identity)
        current = _candidate(root, current_id)
        if (datetime.fromisoformat(pinned[0].created_at.replace("Z", "+00:00")) > datetime.fromisoformat(current[0].created_at.replace("Z", "+00:00")) or pinned[1]["pool_id"] != current[1]["pool_id"]
            or pinned[1]["canonical_ids"] != current[1]["canonical_ids"]
            or pinned[2] != current[2]):
            raise ValueError("stats pin cohort mismatch")
        return identity, []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SnapshotValidationError) as exc:
        return current_id, [f"stats pin 未通过完整验证：{type(exc).__name__}"]
