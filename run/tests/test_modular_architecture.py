from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from hextech.contracts import (
    AugmentId,
    ChampionId,
    GameContext,
    GenerationId,
    HealthState,
    PresentationMode,
    SessionPhase,
    VisionEpoch,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)
from hextech.contracts.identifiers import InvalidIdentifierError, champion_id, optional_augment_id
from hextech.modules.data.generation import DataSnapshotManifest, DataSnapshotView
from hextech.modules.recommendation import RecommendationPolicy, RecommendationService
from hextech.modules.session import SessionCoordinator
from hextech.modules.session.evidence import build_evidence_bundle
from hextech.modules.vision import GameWindowObservation
from hextech.interfaces.overlay.session_adapter import build_runtime_session


@pytest.mark.parametrize("value", [24, "24", "024", 24.0])
def test_champion_id_normalizes_adapter_shapes(value: object) -> None:
    assert champion_id(value) == ChampionId("24")


@pytest.mark.parametrize("value", [True, None, 0, -1, 1.5, float("inf"), "24.0", "jax", []])
def test_champion_id_rejects_ambiguous_values(value: object) -> None:
    with pytest.raises(InvalidIdentifierError):
        champion_id(value)


def _snapshot_view(*, degraded: bool = False) -> DataSnapshotView:
    manifest = DataSnapshotManifest(
        schema_version=2,
        generation_id="g1",
        created_at="now",
        content_fingerprint="a" * 64,
        source_files=(),
        champion_count=2,
        augment_count=1,
        stat_record_count=2,
        files=(),
    )
    return DataSnapshotView(
        manifest,
        {
            "champions": [
                {"id": "24", "name": "武器大师", "英雄胜率": 0.52},
                {"id": "245", "name": "时间刺客", "英雄胜率": 0.61},
            ],
            "champion_hextech": {
                "武器大师": {"hero_id": "24", "augments": [{"id": "100", "win_rate": 0.6}]},
                "时间刺客": {"hero_id": "245", "augments": [{"id": "100", "win_rate": 0.5}]},
            },
            "overlay_hints": {},
            "identities": {
                "augments": {"100": "测试强化"},
                "augment_aliases": {"stable_ready": "100", "测试强化": "100"},
                "catalog_augments": {
                    "stable_ready": {
                        "vision_id": "stable_ready",
                        "name": "测试强化",
                        "tier": "黄金",
                        "canonical_id": "100",
                        "stats_available": True,
                    },
                    "catalog_only": {
                        "vision_id": "catalog_only",
                        "name": "源站缺失强化",
                        "tier": "白银",
                        "canonical_id": "",
                        "stats_available": False,
                    },
                },
            },
        },
        degraded=degraded,
    )


def test_recommendation_uses_snapshot_query_and_preserves_roles_while_sorting() -> None:
    context = GameContext(
        session_id="s1",  # type: ignore[arg-type]
        observed_at=1,
        local_champion_id=ChampionId("24"),
        teammate_champion_ids=(ChampionId("245"),),
    )

    model = RecommendationService().build(context, _snapshot_view())

    assert [row["id"] for row in model.champion_candidates] == ["245", "24"]
    assert [row["selection_role"] for row in model.champion_candidates] == ["teammate", "self"]
    assert model.generation_id == GenerationId("g1")


def test_recommendation_distinguishes_ready_source_missing_and_unresolved_augments() -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(
            VisionSlot(0, VisionSlotState.READY, AugmentId("stable_ready")),
            VisionSlot(1, VisionSlotState.READY, AugmentId("catalog_only")),
            VisionSlot(2, VisionSlotState.READY, AugmentId("unknown")),
        ),
    )

    model = RecommendationService().build(context, _snapshot_view(), vision=vision)

    assert [row["status_code"] for row in model.augment_slots] == [
        "READY",
        "SOURCE_STAT_MISSING",
        "IDENTITY_UNRESOLVED",
    ]
    assert model.augment_slots[0]["canonical_augment_id"] == "100"
    assert model.augment_slots[1]["name"] == "源站缺失强化"


def test_recommendation_falls_back_to_confirmed_name_without_inventing_visual_tier() -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(
            VisionSlot(0, VisionSlotState.READY, name="测试强化", recognition_key="测试强化"),
            VisionSlot(1, VisionSlotState.DETECTING),
            VisionSlot(2, VisionSlotState.DETECTING),
        ),
    )

    model = RecommendationService().build(context, _snapshot_view(), vision=vision)

    assert model.augment_slots[0]["status_code"] == "READY"
    assert model.augment_slots[0]["canonical_augment_id"] == "100"
    assert model.augment_slots[0]["tier"] == ""


def test_snapshot_name_resolution_does_not_first_win_ambiguous_catalog_variants() -> None:
    view = _snapshot_view()
    identities = view._payloads["identities"]  # type: ignore[index]
    identities["augment_aliases"] = {}
    identities["catalog_augments"] = {
        "same_gold": {"name": "同名强化", "tier": "黄金", "canonical_id": "100"},
        "same_prismatic": {"name": "同名强化", "tier": "棱彩", "canonical_id": "200"},
    }

    assert view.resolve_augment("同名强化") is None


def test_session_keeps_waiting_overlay_visible_when_context_or_vision_missing() -> None:
    coordinator = SessionCoordinator()
    waiting_context = coordinator.reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=None,
        vision=None,
        recommendation=None,
    )
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    waiting_selection = coordinator.reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=None,
        recommendation=None,
    )

    assert waiting_context.phase is SessionPhase.WAITING_CONTEXT
    assert waiting_context.visibility.should_show is True
    assert waiting_context.visibility.presentation_mode is PresentationMode.WAITING
    assert waiting_selection.phase is SessionPhase.WAITING_SELECTION
    assert waiting_selection.visibility.should_show is True


def test_session_rejects_failed_slot_as_explicit_failed_state() -> None:
    from hextech.contracts import VisionSlot, VisionSlotState

    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(VisionSlot(0, VisionSlotState.FAILED, error_code="timeout"),),
    )
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=vision,
        recommendation=None,
    )

    assert state.phase is SessionPhase.FAILED
    assert state.visibility.presentation_mode is PresentationMode.FAILED
    assert state.health is HealthState.UNAVAILABLE
    assert state.error_code == "detection_failed"


def test_privacy_off_never_carries_combo_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(VisionSlot(0, VisionSlotState.READY, AugmentId("stable_ready")),),
    )

    view = _snapshot_view()
    monkeypatch.setattr(
        view.__class__,
        "get_combo_stats",
        lambda *_args, **_kwargs: pytest.fail("隐私关闭时不得查询组合统计"),
    )

    model = RecommendationService().build(
        context,
        view,
        vision=vision,
        policy=RecommendationPolicy(private_stats_enabled=False),
    )

    assert model.augment_slots[0]["status_code"] == "PRIVACY_OFF"
    assert model.augment_slots[0]["stats"] == {}


def test_privacy_off_has_priority_over_snapshot_and_context_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(VisionSlot(0, VisionSlotState.READY, AugmentId("stable_ready")),),
    )
    policy = RecommendationPolicy(private_stats_enabled=False)
    ready_context = GameContext(
        session_id="s1", observed_at=1, local_champion_id=ChampionId("24")
    )  # type: ignore[arg-type]
    original_status = DataSnapshotView.status
    monkeypatch.setattr(
        DataSnapshotView,
        "status",
        lambda view: {**original_status(view), "state": "unavailable"},
    )

    unavailable_snapshot = RecommendationService().build(ready_context, _snapshot_view(), vision=vision, policy=policy)

    monkeypatch.undo()
    missing_context = RecommendationService().build(
        GameContext(session_id="s1", observed_at=1),  # type: ignore[arg-type]
        _snapshot_view(),
        vision=vision,
        policy=policy,
    )

    assert unavailable_snapshot.augment_slots[0]["status_code"] == "PRIVACY_OFF"
    assert missing_context.augment_slots[0]["status_code"] == "PRIVACY_OFF"


def test_degraded_snapshot_does_not_lower_unavailable_context_health() -> None:
    context = GameContext(
        session_id="s1",  # type: ignore[arg-type]
        observed_at=1,
        local_champion_id=ChampionId("24"),
        health=HealthState.UNAVAILABLE,
        error_code="lcu_disconnected",
    )

    model = RecommendationService().build(context, _snapshot_view(degraded=True))

    assert model.health is HealthState.UNAVAILABLE
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=model.generation_id,
        context=context,
        vision=None,
        recommendation=model,
    )
    assert state.health is HealthState.UNAVAILABLE
    assert state.error_code == "lcu_disconnected"


def test_future_recommendation_fields_report_not_implemented() -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]

    model = RecommendationService().build(context, _snapshot_view())

    assert model.item_recommendations == ()
    assert model.item_recommendations_status == "NOT_IMPLEMENTED"
    assert model.champion_select_augments_status == "NOT_IMPLEMENTED"


def test_runtime_adapter_rejects_malformed_numbers_without_losing_frame() -> None:
    state = build_runtime_session(
        event={
            "generated_at": "bad-time",
            "source": {"session_id": "s1", "selection_epoch": "bad-epoch", "scene_state": "active"},
            "slots": [{"state": "ready", "augment_id": "24.0", "confidence": "bad"}],
        },
        context_payload={"ok": True, "session_id": "s1", "champion_id": 24, "generated_at": "bad-time"},
        snapshot_view=_snapshot_view(),
        user_enabled=True,
        game_present=True,
    )

    assert state.session_id == "s1"
    assert state.vision is None
    assert optional_augment_id("24.0") is None


def test_typed_renderer_preserves_context_expired_status() -> None:
    from hextech.interfaces.overlay.renderer import build_render_model_from_session

    state = build_runtime_session(
        event={},
        context_payload={
            "ok": False,
            "session_id": "s1",
            "error": "context_expired",
            "generated_at": "bad-time",
        },
        snapshot_view=_snapshot_view(),
        user_enabled=True,
        game_present=True,
    )

    rendered = build_render_model_from_session(state)
    assert {row["status_code"] for row in rendered["stats"]} == {"CONTEXT_EXPIRED"}


def test_session_without_context_or_vision_uses_nonempty_unbound_id() -> None:
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=None,
        vision=None,
        recommendation=None,
    )

    assert state.session_id == "unbound"


def test_typed_game_context_provider_is_the_lcu_runtime_boundary() -> None:
    from hextech.modules.game_context import TypedGameContextProvider

    provider = TypedGameContextProvider(ttl_seconds=8)
    context = provider.update(
        {
            "localPlayerCellId": 1,
            "myTeam": [
                {"cellId": 1, "championId": 24},
                {"cellId": 2, "championId": 245},
            ],
            "benchChampions": [{"championId": 86}],
        },
        now=10,
        source="overlay-lcu",
    )

    assert context.local_champion_id == ChampionId("24")
    assert context.teammate_champion_ids == (ChampionId("245"),)
    assert context.bench_champion_ids == (ChampionId("86"),)
    assert context.source == "overlay-lcu"


def test_game_window_observation_prevents_mixed_capture_geometry() -> None:
    GameWindowObservation(1, 1, (0, 0, 1920, 1080), (1920, 1080), 1.25, True, (1, 1, 0, 0))
    with pytest.raises(ValueError, match="capture_client_size_mismatch"):
        GameWindowObservation(1, 1, (0, 0, 1920, 1080), (2560, 1600), 1.25, True, (1, 1, 0, 0))


def test_core_dependency_boundaries() -> None:
    root = Path(__file__).parents[1] / "src" / "hextech"
    allowed_dependencies = {
        "contracts": {"contracts"},
        "modules": {"contracts", "modules"},
        "interfaces": {"contracts", "modules", "interfaces"},
        "infrastructure": {"contracts", "modules", "infrastructure"},
        "runtime": {"contracts", "modules", "runtime"},
        "bootstrap": {"contracts", "modules", "interfaces", "infrastructure", "runtime", "bootstrap"},
    }
    violations: list[str] = []
    for path in root.rglob("*.py"):
        source_layer = path.relative_to(root).parts[0]
        if source_layer not in allowed_dependencies:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        for imported in imports:
            parts = imported.split(".")
            if len(parts) < 2 or parts[0] != "hextech":
                continue
            target_layer = parts[1]
            if target_layer in allowed_dependencies and target_layer not in allowed_dependencies[source_layer]:
                violations.append(f"{path.relative_to(root)}:{imported}")
    assert not violations


def test_desktop_matches_float_lcu_id_to_string_snapshot_id() -> None:
    from hextech.interfaces.desktop.app import HextechUI

    ui = HextechUI.__new__(HextechUI)
    rows = ui._build_candidate_display_list(
        {
            "selected_champion_ids": [24.0],
            "local_champion_id": 24.0,
            "teammate_champion_ids": [],
            "bench_champion_ids": [],
        },
        [{"id": "24", "name": "武器大师", "英雄胜率": 0.527844, "英雄出场率": 0.004588}],
    )

    assert rows == [
        {"id": "24", "name": "武器大师", "win": 0.527844, "pick": 0.004588, "tier": "T3", "selection_role": "self"}
    ]


def test_evidence_bundle_requires_one_generation_and_session() -> None:
    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    model = RecommendationService().build(context, _snapshot_view())
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(2),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(
            VisionSlot(0, VisionSlotState.READY, AugmentId("stable_ready")),
            VisionSlot(1, VisionSlotState.READY, AugmentId("stable_ready")),
            VisionSlot(2, VisionSlotState.READY, AugmentId("stable_ready")),
        ),
    )
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=vision,
        recommendation=model,
    )

    evidence = build_evidence_bundle(
        state,
        lcu_summary={"team": [{"champion_id": "24", "raw_type": "float"}]},
        window_summary={
            "hwnd": 1,
            "client_size": [1920, 1080],
            "capture_size": [1920, 1080],
            "dpi_scale": 1.25,
        },
    )

    assert evidence.session_id == "s1"
    assert evidence.generation_id == "g1"
    assert evidence.final_state["vision_epoch"] == 2
    assert evidence.final_state["local_champion_id"] == "24"


def test_runtime_adapter_drives_recommendation_and_session_production_chain() -> None:
    state = build_runtime_session(
        event={
            "generated_at": 2,
            "source": {"session_id": "s1", "selection_epoch": 3, "scene_state": "active"},
            "slots": [
                {"state": "ready", "augment_id": "stable_ready"},
                {"state": "failed", "diagnostic": "timeout"},
                {"state": "detecting"},
            ],
        },
        context_payload={
            "ok": True,
            "session_id": "s1",
            "champion_id": 24.0,
            "generated_at": 1,
            "source": "lcu",
        },
        snapshot_view=_snapshot_view(),
        user_enabled=True,
        game_present=True,
    )

    assert state.recommendation is not None
    assert state.recommendation.generation_id == GenerationId("g1")
    assert [row["status_code"] for row in state.recommendation.augment_slots] == [
        "READY",
        "RECOGNITION_MISSING",
        "RECOGNITION_MISSING",
    ]
    assert state.vision is not None and int(state.vision.epoch) == 3


def test_typed_overlay_renderer_reads_published_chinese_stat_fields() -> None:
    from hextech.interfaces.overlay.renderer import build_render_model_from_session

    context = GameContext(session_id="s1", observed_at=1, local_champion_id=ChampionId("24"))  # type: ignore[arg-type]
    vision = VisionSelection(
        session_id="s1",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(
            VisionSlot(0, VisionSlotState.READY, AugmentId("stable_ready")),
            VisionSlot(1, VisionSlotState.DETECTING),
            VisionSlot(2, VisionSlotState.FAILED),
        ),
    )
    view = _snapshot_view()
    published_row = view._payloads["champion_hextech"]["武器大师"]["augments"][0]  # type: ignore[index]
    published_row.pop("win_rate", None)
    published_row.update(
        {"海克斯胜率": 0.6, "海克斯出场率": 0.08}
    )
    model = RecommendationService().build(context, view, vision=vision)
    state = SessionCoordinator().reduce(
        user_enabled=True,
        game_present=True,
        generation_id=GenerationId("g1"),
        context=context,
        vision=vision,
        recommendation=model,
    )

    rendered = build_render_model_from_session(state)
    assert rendered["stats"][0]["winrate_text"] == "60.0%"
    assert rendered["stats"][0]["pickrate_text"] == "8.0%"
    assert rendered["stats"][1]["status_code"] == "DETECTING"
    assert rendered["stats"][2]["status_code"] == "RECOGNITION_MISSING"

    degraded_slots = tuple(
        ({**row, "status_code": "GENERATION_DEGRADED"} if index == 0 else row)
        for index, row in enumerate(model.augment_slots)
    )
    degraded_state = replace(state, recommendation=replace(model, augment_slots=degraded_slots))
    degraded_rendered = build_render_model_from_session(degraded_state)
    assert degraded_rendered["stats"][0]["stats_text"] == "胜率 60.0% · 出场 8.0%"
    assert degraded_rendered["stats"][0]["status_code"] == "GENERATION_DEGRADED"
    assert degraded_rendered["stats"][0]["status_text"] == "上一代数据"
