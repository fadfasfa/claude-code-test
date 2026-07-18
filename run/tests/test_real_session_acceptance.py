from __future__ import annotations

import json
from pathlib import Path

import pytest

from hextech.contracts import (
    AugmentId,
    ChampionId,
    GameContext,
    GenerationId,
    VisionEpoch,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)
from hextech.modules.recommendation import RecommendationService
from hextech.modules.session import SessionCoordinator
from hextech.modules.session.evidence import build_evidence_bundle, write_evidence_bundle
from tests.test_modular_architecture import _snapshot_view

from tooling.acceptance.verify_data_pipeline import AcceptanceFailure, verify_real_session_evidence


def _evidence(tmp_path: Path) -> Path:
    screenshot = tmp_path / "overlay.png"
    screenshot.write_bytes(b"png")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "evidence_kind": "real_game_session",
                "generation_id": "g1",
                "session_id": "s1",
                "lcu": {"session_id": "s1", "local_champion_id": "24"},
                "window": {
                    "session_id": "s1",
                    "hwnd": 100,
                    "client_size": [2560, 1600],
                    "capture_size": [2560, 1600],
                    "dpi_scale": 1.5,
                },
                "vision": {"session_id": "s1", "epoch": 2, "slots": [{}, {}, {}]},
                "recommendation": {"session_id": "s1", "generation_id": "g1"},
                "final_state": {
                    "session_id": "s1",
                    "generation_id": "g1",
                    "vision_epoch": 2,
                    "should_show": True,
                    "presentation_mode": "content",
                },
                "screenshot": "overlay.png",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_real_session_evidence_accepts_consistent_five_link_bundle(tmp_path: Path) -> None:
    result = verify_real_session_evidence(_evidence(tmp_path), expected_generation_id="g1")
    assert result["session_id"] == "s1"
    assert result["vision_epoch"] == 2


def test_real_session_evidence_rejects_synthetic_or_mixed_session(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["vision"]["session_id"] = "other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AcceptanceFailure, match="session"):
        verify_real_session_evidence(path, expected_generation_id="g1")

    payload["vision"]["session_id"] = "s1"
    payload["evidence_kind"] = "synthetic"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AcceptanceFailure, match="真实游戏会话"):
        verify_real_session_evidence(path, expected_generation_id="g1")


def test_acceptance_reads_only_complete_generation() -> None:
    run_dir = Path(__file__).resolve().parents[1]
    source = (run_dir / "tooling" / "acceptance" / "verify_data_pipeline.py").read_text(encoding="utf-8")

    assert "DataSnapshotClient(root).open_view()" in source
    assert "verify_generation(snapshot_root)" in source
    assert "publish(" not in source


def test_production_evidence_builder_matches_acceptance_verifier(tmp_path: Path) -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(2),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=tuple(
            VisionSlot(index, VisionSlotState.READY, AugmentId("stable_ready")) for index in range(3)
        ),
    )
    model = RecommendationService().build(context, _snapshot_view(), vision=vision)
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=vision,
        recommendation=model,
    )
    screenshot = tmp_path / "overlay.png"
    screenshot.write_bytes(b"png")
    bundle = build_evidence_bundle(
        state,
        lcu_summary={"local_champion_id": "24"},
        window_summary={
            "hwnd": 100,
            "client_size": [1920, 1080],
            "capture_size": [1920, 1080],
            "dpi_scale": 1.25,
        },
        screenshot=screenshot.name,
    )
    path = write_evidence_bundle(bundle, tmp_path / "session.json")

    result = verify_real_session_evidence(path, expected_generation_id="g1")
    assert result["session_id"] == "s1"
