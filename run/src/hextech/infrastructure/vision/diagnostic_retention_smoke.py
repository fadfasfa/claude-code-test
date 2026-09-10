"""冻结包内部 Timeline v1/v2 与留存所有权烟测。"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention
from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter
from hextech.infrastructure.vision.sidecar_diagnostics import write_selection_timeline_observation
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.session.build_identity import current_build_id


def _event(*, terminal: bool = False) -> dict[str, Any]:
    now = time.time()
    reason = "gameflow_ended" if terminal else "active"
    return {
        "schema_version": 3,
        "build_id": current_build_id(),
        "active": not terminal,
        "visible": not terminal,
        "selection_type": "hextech",
        "source": {
            "build_id": current_build_id(),
            "sidecar_pid": os.getpid(),
            "sidecar_instance_id": f"packaged-retention-smoke-{os.getpid()}",
            "session_id": "packaged-retention-smoke",
            "game_instance_id": "packaged-retention-smoke-game",
            "selection_epoch": 1,
            "selection_revision": 1,
            "scene_state": "paused" if terminal else "active",
            "selection_window_active": not terminal,
            "reason": reason,
            "game_window_mode": {
                "status": "supported",
                "mode": "borderless",
                "reason": "window_mode_borderless",
                "source": "game_cfg",
                "observed_at": now,
            },
        },
        "timing": {
            "observation_kind": "visibility_probe" if terminal else "recognition",
            "capture_status": "not_captured" if terminal else "captured",
            "capture_started_at": now - 0.03,
            "captured_at": now - 0.02,
            "recognition_completed_at": now - 0.01,
            "event_written_at": now,
        },
        "slots": [
            {
                "slot": slot,
                "state": "ready",
                "augment_id": f"smoke-{slot}",
                "name": f"留存烟测 {slot + 1}",
                "slot_generation": 1,
            }
            for slot in range(3)
        ],
    }


def run_diagnostic_retention_smoke(runtime_root: str | Path | None = None) -> dict[str, Any]:
    """预置 v1、append v2 terminal，并用真实留存器连续执行两轮。"""

    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    timeline_root = root / "state" / "overlay_vision_timelines"
    timeline_root.mkdir(parents=True, exist_ok=True)
    for index in range(20):
        legacy = timeline_root / f"selection-packaged-legacy-e{index:04d}.jsonl"
        legacy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "selection_epoch": index + 1,
                    "source_reason": "legacy_packaged_fixture",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    trace_path = root / "state" / "overlay_vision_trace.v1.json"
    writer = VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=write_selection_timeline_observation,
    )
    writer.submit(_event(), trace_path, write_trace=False)
    writer.submit(_event(terminal=True), trace_path, write_trace=False)
    writer.close(timeout=5.0)
    writer_status = writer.status()

    first_retention = apply_diagnostic_retention(root, force=True)
    second_retention = apply_diagnostic_retention(root, force=True)
    legacy_paths = list(timeline_root.glob("selection-packaged-legacy-*.jsonl"))
    v2_paths: list[Path] = []
    terminal_found = False
    for path in timeline_root.glob("selection-*.jsonl"):
        try:
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if entries and entries[0].get("schema_version") == 2:
            v2_paths.append(path)
            terminal_found = terminal_found or any(
                entry.get("source_reason") == "gameflow_ended" for entry in entries
            )

    first_last_run = deepcopy(first_retention.get("last_run") or {})
    second_last_run = deepcopy(second_retention.get("last_run") or {})
    checks = {
        "legacy_v1_retained": len(legacy_paths) == 20,
        "v2_retained": len(v2_paths) >= 1,
        "terminal_marker_retained": terminal_found,
        "writer_append_confirmed": bool(writer_status["current_timeline_path_hash"])
        and float(writer_status["timeline_last_written_at"]) > 0.0
        and int(writer_status["timeline_epoch"]) == 1,
        "writer_no_failure": int(writer_status["timeline_failed_count"]) == 0,
        "first_retention_completed": first_last_run.get("disposition") == "completed"
        and int(first_last_run.get("error_count") or 0) == 0,
        "second_retention_completed": second_last_run.get("disposition") == "completed"
        and int(second_last_run.get("error_count") or 0) == 0,
    }
    return {
        "ok": all(checks.values()),
        "build_id": current_build_id(),
        "checks": checks,
        "legacy_v1_count": len(legacy_paths),
        "v2_count": len(v2_paths),
        "writer": writer_status,
        "first_retention": first_last_run,
        "second_retention": second_last_run,
    }


__all__ = ["run_diagnostic_retention_smoke"]
