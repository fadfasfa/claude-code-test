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
        render_summary={"rows": [{"slot": index, "name": "测试", "status_code": "READY", "stats_text": "胜率 50.0% · 出场 1.0%"} for index in range(3)]},
    )
    path = write_evidence_bundle(bundle, tmp_path / "session.json")

    result = verify_real_session_evidence(path, expected_generation_id="g1")
    assert result["session_id"] == "s1"
    assert result["selection_revision"] == 1
    assert result["render_signature"] == bundle.render_signature


def test_v2_evidence_rejects_render_signature_or_revision_mismatch(tmp_path: Path) -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(2),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=tuple(VisionSlot(index, VisionSlotState.READY, AugmentId("stable_ready")) for index in range(3)),
        selection_revision=2,
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
            "client_size": [2560, 1600],
            "capture_size": [2560, 1600],
            "dpi_scale": 1.5,
        },
        screenshot=screenshot.name,
        render_summary={"rows": [{"slot": index, "name": "测试", "status_code": "READY", "stats_text": "胜率 50.0% · 出场 1.0%"} for index in range(3)]},
    )
    path = write_evidence_bundle(bundle, tmp_path / "session.v2.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["render"]["selection_revision"] = 3
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AcceptanceFailure, match="revision"):
        verify_real_session_evidence(path, expected_generation_id="g1")

    payload["render"]["selection_revision"] = 2
    payload["render_signature"] = "tampered"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AcceptanceFailure, match="signature"):
        verify_real_session_evidence(path, expected_generation_id="g1")


def test_real_session_capture_waits_two_ticks_and_cancels_changed_signature(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image

    from hextech.interfaces.overlay import host_sync

    class FakeRoot:
        def __init__(self) -> None:
            self.callbacks: list[tuple[int, object]] = []

        def after(self, delay: int, callback) -> None:
            self.callbacks.append((delay, callback))

        def update_idletasks(self) -> None:
            return None

        def winfo_rootx(self) -> int:
            return 0

        def winfo_rooty(self) -> int:
            return 0

        def winfo_width(self) -> int:
            return 800

        def winfo_height(self) -> int:
            return 500

    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(2),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=tuple(
            VisionSlot(index, VisionSlotState.READY, AugmentId("stable_ready"), name=f"卡名 {index}")
            for index in range(3)
        ),
        selection_revision=1,
    )
    recommendation = RecommendationService().build(context, _snapshot_view(), vision=vision)
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=vision,
        recommendation=recommendation,
    )
    rows = [
        {"slot": index, "name": f"卡名 {index}", "status_code": "READY", "stats_text": "胜率 50.0% · 出场 1.0%"}
        for index in range(3)
    ]
    model = {"stats": rows, "synergies": []}
    snapshot = {
        "slots": [{"slot": index, "name": f"卡名 {index}"} for index in range(3)],
        "_acceptance_rules": ["dual_font:2", "dual_font:2", "strong_text:2"],
        "source": {
            "window_hwnd": 100,
            "client_rect": [0, 0, 2560, 1600],
            "capture_size": [2560, 1600],
            "dpi_scale": 1.5,
        },
    }
    evidence_dir = tmp_path / "session_evidence"
    monkeypatch.setattr(host_sync, "overlay_runtime_state_path", lambda _name: evidence_dir)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda **_kwargs: Image.new("RGB", (800, 500), "black"))
    root = FakeRoot()
    visibility: dict[str, object] = {"window_visible": True}

    host_sync._write_real_session_evidence(root, state, snapshot, model, visibility)
    assert root.callbacks == []
    host_sync._write_real_session_evidence(root, state, snapshot, model, visibility)
    assert [delay for delay, _callback in root.callbacks] == [100]

    visibility["current_render_signature"] = "changed-before-capture"
    root.callbacks.pop()[1]()  # type: ignore[operator]
    assert not evidence_dir.exists()
    assert "evidence_attempt_key" not in visibility


def test_real_session_capture_writes_each_revision_and_updates_latest(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image

    from hextech.interfaces.overlay import host_sync

    class FakeRoot:
        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def after(self, delay: int, callback) -> None:
            assert delay == 100
            self.callbacks.append(callback)

        def update_idletasks(self) -> None:
            return None

        def winfo_rootx(self) -> int:
            return 0

        def winfo_rooty(self) -> int:
            return 0

        def winfo_width(self) -> int:
            return 800

        def winfo_height(self) -> int:
            return 500

    evidence_dir = tmp_path / "session_evidence"
    monkeypatch.setattr(host_sync, "overlay_runtime_state_path", lambda _name: evidence_dir)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda **_kwargs: Image.new("RGB", (800, 500), "black"))
    root = FakeRoot()
    visibility: dict[str, object] = {"window_visible": True}
    snapshot = {
        "slots": [
            {
                "slot": index,
                "name": f"卡名 {index}",
                "acceptance_rule": "observed_name:2",
                "channels": {
                    "text": {
                        "top_candidates": [
                            {"augment_id": f"candidate-{index}", "name": f"卡名 {index}", "confidence": 0.93}
                        ],
                        "margin": 0.09,
                    },
                    "text_alt": {"top_candidates": [], "margin": 0.03},
                    "icon": {"top_candidates": [], "margin": 0.02},
                    "icon_shortlist": {"top_candidates": [], "group_count": 2},
                    "observed_name": {
                        "top_candidates": [
                            {"augment_id": f"canonical-{index}", "name": f"卡名 {index}", "confidence": 0.97}
                        ],
                        "margin": 0.12,
                    },
                },
            }
            for index in range(3)
        ],
        "_acceptance_rules": ["observed_name:2"] * 3,
        "source": {
            "window_hwnd": 100,
            "client_rect": [0, 0, 2560, 1600],
            "capture_size": [2560, 1600],
            "dpi_scale": 1.5,
        },
    }

    for revision, context_revision in ((1, 1), (1, 2), (2, 2)):
        context = GameContext(
            session_id="s1",  # type: ignore[arg-type]
            observed_at=1,
            local_champion_id=ChampionId("24"),
            game_instance_id="s1",
            window_hwnd=100,
            context_revision=context_revision,
        )
        vision = VisionSelection(
            session_id="s1",  # type: ignore[arg-type]
            epoch=VisionEpoch(2),
            observed_at=2,
            scene_state=VisionSceneState.ACTIVE,
            slots=tuple(
                VisionSlot(index, VisionSlotState.READY, AugmentId("stable_ready"), name=f"卡名 {index}")
                for index in range(3)
            ),
            selection_revision=revision,
        )
        recommendation = RecommendationService().build(context, _snapshot_view(), vision=vision)
        state = SessionCoordinator().reduce(
            user_enabled=True,
            game_present=True,
            generation_id=GenerationId("g1"),
            context=context,
            vision=vision,
            recommendation=recommendation,
        )
        rows = [
            {"slot": index, "name": f"卡名 {index}", "status_code": "READY", "stats_text": f"胜率 5{revision}.0% · 出场 {context_revision}.0%"}
            for index in range(3)
        ]
        model = {"stats": rows, "synergies": []}
        host_sync._write_real_session_evidence(root, state, snapshot, model, visibility)
        host_sync._write_real_session_evidence(root, state, snapshot, model, visibility)
        root.callbacks.pop()()  # type: ignore[operator]

    first = list(evidence_dir.glob("overlay-s1-e2-r1-c1-*.v2.json"))
    context_changed = list(evidence_dir.glob("overlay-s1-e2-r1-c2-*.v2.json"))
    second = list(evidence_dir.glob("overlay-s1-e2-r2-c2-*.v2.json"))
    assert len(first) == len(context_changed) == len(second) == 1
    latest = json.loads((evidence_dir / "latest_real_session.v2.json").read_text(encoding="utf-8"))
    assert latest["selection_revision"] == 2
    assert latest["render"]["acceptance_rules"] == ["observed_name:2"] * 3
    assert [slot["acceptance_rule"] for slot in latest["render"]["event_slots"]] == [
        "observed_name:2"
    ] * 3
    assert latest["render"]["event_slots"][0]["channels"]["observed_name"] == {
        "top_candidates": [
            {"augment_id": "canonical-0", "name": "卡名 0", "confidence": 0.97}
        ],
        "margin": 0.12,
    }
    assert set(latest["render"]["event_slots"][0]["channels"]) == {
        "text",
        "text_alt",
        "icon",
        "icon_shortlist",
        "observed_name",
    }
    assert latest["screenshot"] == ""
    assert not list(evidence_dir.glob("*.png"))
