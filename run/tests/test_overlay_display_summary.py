"""展示摘要必须保持原文身份，并对最终排版给出可核验结果。"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from types import SimpleNamespace

from hextech.interfaces.overlay.renderer import build_render_model
from hextech.modules.recommendation.synergy_projection import project_generation_synergy


def _snapshot() -> dict:
    return {
        "source": {"selection_window_active": True},
        "slots": [
            {
                "slot": 0,
                "state": "ready",
                "augment_id": "augment-a",
                "name": "升级：无尽之刃",
                "tier": "Gold",
            }
        ],
    }


def _hint_cache(content: object) -> dict:
    return {
        "snapshot": {"state": "ready"},
        "hints": {
            "augment-a": {
                "augment_id": "augment-a",
                "name": "升级：无尽之刃",
                "tier": "Gold",
                "synergies": [
                    {
                        "hero_id": "1",
                        "hero_name": "测试英雄",
                        "rating": "S",
                        "tag": "联动",
                        "content": content,
                    }
                ],
            }
        },
        "name_index": {},
    }


def _display_spec(*, width: int = 476, lines: int = 4) -> dict:
    return {
        "id": "compact-1920x1080-v1",
        "max_width_px": width,
        "font_px": 18,
        "max_lines": lines,
        "prefix": "测试英雄 · S · 联动 · ",
    }


def test_renderer_does_not_apply_the_historical_180_character_cut() -> None:
    raw = "a" * 181 + "最终条件必须保留。"

    model = build_render_model(
        _snapshot(),
        hint_cache=_hint_cache(raw),
        context={"ok": True, "champion_id": "1", "champion_name": "测试英雄"},
    )

    assert model["synergies"][0]["content"] == raw
    assert model["synergies"][0]["content"].endswith("最终条件必须保留。")


def test_projection_preserves_raw_content_and_identity_counts() -> None:
    raw = "当生命值低于 30% 时，伤害提高 12%。  当生命值低于 30% 时，伤害提高 12%。"
    payload = {
        "1": {
            "name": "测试英雄",
            "synergy_items": [
                {
                    "augment_names": ["升级：无尽之刃"],
                    "content": raw,
                    "rating": "S",
                    "tag": "联动",
                }
            ],
        }
    }
    cache = {
        "source": {},
        "hints": {"augment-a": {"augment_id": "augment-a", "name": "升级：无尽之刃"}},
        "name_index": {"升级：无尽之刃": "augment-a"},
    }
    original = deepcopy(payload)

    report = project_generation_synergy(cache, payload)

    assert payload == original
    assert cache["hints"]["augment-a"]["synergies"][0]["content"] == raw
    assert report["item_count"] == 1
    assert report["projected_name_count"] == 1
    assert report["projection_coverage"] == 1.0


def test_projection_dedup_signature_is_unchanged_by_raw_preservation() -> None:
    first = "命中后提高  15% 伤害。"
    payload = {
        "1": {
            "name": "测试英雄",
            "synergy_items": [
                {"augment_names": ["测试强化"], "content": first, "rating": "S", "tag": "联动"},
                {"augment_names": ["测试强化"], "content": "命中后提高 15% 伤害。", "rating": "S", "tag": "联动"},
            ],
        }
    }
    cache = {
        "source": {},
        "hints": {"augment-a": {"augment_id": "augment-a", "name": "测试强化"}},
        "name_index": {"测试强化": "augment-a"},
    }

    report = project_generation_synergy(cache, payload)

    assert report["item_count"] == 2
    assert len(cache["hints"]["augment-a"]["synergies"]) == 1
    assert cache["hints"]["augment-a"]["synergies"][0]["content"] == first


def test_projection_keeps_legacy_none_and_empty_content_dedup_semantics() -> None:
    payload = {
        "1": {
            "name": "测试英雄",
            "synergy_items": [
                {"augment_names": ["测试强化"], "content": None, "rating": "S", "tag": "联动"},
                {"augment_names": ["测试强化"], "content": "", "rating": "S", "tag": "联动"},
            ],
        }
    }
    cache = {
        "source": {},
        "hints": {"augment-a": {"augment_id": "augment-a", "name": "测试强化"}},
        "name_index": {"测试强化": "augment-a"},
    }

    project_generation_synergy(cache, payload)

    assert len(cache["hints"]["augment-a"]["synergies"]) == 1
    assert cache["hints"]["augment-a"]["synergies"][0]["content"] is None


def test_display_summary_cleanup_is_idempotent_and_semantics_safe() -> None:
    from hextech.modules.recommendation.display_summary import clean_display_content

    raw = (
        "当生命值低于30%时，伤害提高12%。当生命值低于30%时，伤害提高12%。"
        "当生命值不低于30%时，伤害不会提高12%。点击查看详情。"
    )

    once = clean_display_content(raw)
    twice = clean_display_content(once.text)

    assert once.text == twice.text
    assert once.text == "当生命值低于30%时，伤害提高12%。当生命值不低于30%时，伤害不会提高12%。"
    assert once.removed_duplicate_sentences == 1
    assert once.removed_boilerplate_sentences == 1
    assert "30%" in once.text and "12%" in once.text
    assert "不低于" in once.text and "不会" in once.text


def test_same_sentence_under_different_conditions_is_not_merged() -> None:
    from hextech.modules.recommendation.display_summary import clean_display_content

    raw = "对A：提高伤害。优先选择。对B：降低冷却。优先选择。"

    assert clean_display_content(raw).text == raw


def test_mixed_english_sentence_spacing_is_preserved() -> None:
    from hextech.modules.recommendation.display_summary import clean_display_content

    raw = "Proc only at 30% HP! Do not trigger otherwise."

    assert clean_display_content(raw).text == raw


def test_summary_metadata_is_source_and_layout_bound_without_mutation() -> None:
    from hextech.modules.recommendation.display_summary import (
        DisplaySummaryCache,
        RULE_VERSION,
        ensure_display_summary,
    )

    item = {"content": "造成15%额外伤害，但仅在目标被控制时生效。", "source": "apex"}
    original = deepcopy(item)
    cache = DisplaySummaryCache(max_entries=2)

    first = ensure_display_summary(item, display_spec=_display_spec(), cache=cache)
    second = ensure_display_summary(first, display_spec=_display_spec(), cache=cache)

    assert item == original
    assert first == second
    assert first["content"] == original["content"]
    summary = first["display_summary"]
    assert summary["rule_version"] == RULE_VERSION
    assert summary["display_spec"] == _display_spec()
    assert len(summary["source_sha256"]) == 64
    assert summary["status"] == "ready"
    assert cache.size == 1


def test_over_budget_summary_is_not_partially_cut_or_recast_as_praise() -> None:
    from hextech.modules.recommendation.display_summary import build_display_summary

    raw = "只有在目标被控制至少2秒且生命值低于30%时，才会造成125点额外伤害；否则不会触发。"

    summary = build_display_summary(raw, display_spec=_display_spec(width=80, lines=1))

    assert summary["status"] == "review_required"
    assert summary["text"] == ""
    assert summary["omission_reason"] == "complete_content_exceeds_budget"
    assert summary["source_text"] == raw
    assert summary["original_char_count"] == len(raw)
    assert "推荐" not in summary["fallback_text"]
    assert "强" not in summary["fallback_text"]


def test_budget_is_measured_per_spec_and_not_a_universal_72_character_cap() -> None:
    from hextech.modules.recommendation.display_summary import build_display_summary

    raw = "a" * 120

    roomy = build_display_summary(raw, display_spec=_display_spec(width=476, lines=4))
    narrow = build_display_summary(raw, display_spec=_display_spec(width=80, lines=1))

    assert roomy["status"] == "ready"
    assert len(roomy["text"]) == 120
    assert narrow["status"] == "review_required"


def test_non_text_content_fails_explicitly_and_preserves_raw_value() -> None:
    from hextech.modules.recommendation.display_summary import ensure_display_summary

    raw = {"paragraphs": ["需要完整条件"], "threshold": "30%"}
    item = {"content": raw}

    prepared = ensure_display_summary(item, display_spec=_display_spec())

    assert prepared["content"] == raw
    assert prepared["display_summary"]["status"] == "review_required"
    assert prepared["display_summary"]["omission_reason"] == "unsupported_content_type"


def test_cache_is_bounded_and_invalidates_for_raw_or_spec_identity() -> None:
    from hextech.modules.recommendation.display_summary import DisplaySummaryCache

    cache = DisplaySummaryCache(max_entries=2)
    first = cache.build("条件 A 生效。", display_spec=_display_spec())
    second = cache.build("条件 A 生效。", display_spec=_display_spec(lines=5))
    third = cache.build("条件 B 不生效。", display_spec=_display_spec(lines=5))

    assert first["source_sha256"] != third["source_sha256"]
    assert first["display_spec"] != second["display_spec"]
    assert cache.size == 2


def test_stale_rule_version_is_not_reused() -> None:
    from hextech.modules.recommendation.display_summary import RULE_VERSION, ensure_display_summary

    item = {
        "content": "仅在拥有2件装备时，提高15%伤害。",
        "display_summary": {
            "rule_version": "obsolete-rule",
            "source_sha256": "0" * 64,
            "display_spec": _display_spec(),
            "status": "ready",
            "text": "错误旧摘要",
            "truncated": False,
        },
    }

    prepared = ensure_display_summary(item, display_spec=_display_spec())

    assert prepared["display_summary"]["rule_version"] == RULE_VERSION
    assert prepared["display_summary"]["text"] != "错误旧摘要"


def test_short_legacy_content_is_prepared_for_display_without_changing_raw() -> None:
    from hextech.interfaces.overlay.text_metrics import prepare_synergy_display_summaries
    from hextech.modules.recommendation.display_summary import DisplaySummaryCache

    raw = "仅在技能命中后，提高15%伤害。"
    model = {
        "stats": [],
        "synergies": [
            {
                "slot": 0,
                "augment_name": "升级：无尽之刃",
                "hero_name": "测试英雄",
                "rating": "S",
                "tag": "联动",
                "tier": "Gold",
                "content": raw,
            }
        ],
    }

    prepared = prepare_synergy_display_summaries(
        model,
        viewport_size=(1920, 1080),
        display_mode="compact",
        cache=DisplaySummaryCache(max_entries=4),
    )

    row = prepared["synergies"][0]
    assert row["raw_content"] == raw
    assert row["content"] == raw
    assert row["display_summary"]["status"] == "ready"
    assert "display_summary" not in model["synergies"][0]


def test_background_preparation_key_includes_viewport_and_display_mode() -> None:
    from hextech.interfaces.overlay.host_data_preparation import preparation_key

    event = _snapshot()
    context = {"ok": True, "champion_id": "1"}

    compact = preparation_key(
        event,
        context,
        0,
        viewport_size=(1920, 1080),
        display_mode="compact",
    )
    expanded = preparation_key(
        event,
        context,
        0,
        viewport_size=(2560, 1600),
        display_mode="expanded",
    )

    assert compact != expanded
    assert compact[-2:] == ((1920, 1080), "compact")
    assert expanded[-2:] == ((2560, 1600), "expanded")


def test_legacy_summary_derivation_runs_on_the_single_preparation_worker(monkeypatch) -> None:
    from hextech.interfaces.overlay import host_data_preparation as module

    worker_threads: list[int] = []
    original_prepare = module.prepare_synergy_display_summaries

    def observed_prepare(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(module, "prepare_synergy_display_summaries", observed_prepare)
    monkeypatch.setattr(
        module,
        "build_runtime_session",
        lambda **kwargs: SimpleNamespace(event=kwargs["event"]),
    )
    monkeypatch.setattr(
        module,
        "build_render_model_from_session",
        lambda *_args, **_kwargs: _canvas_model("仅在命中后提高15%伤害。"),
    )
    monkeypatch.setattr(module.OverlayStageRuntime, "project", lambda state, *_args: state)

    class View:
        def status(self):
            return {"generation_id": "generation-a"}

        def get_overlay_display_hints(self):
            return {"source": {}, "hints": {}, "name_index": {}}

    class Source:
        def open_view(self):
            return View()

        def read_hint_cache(self):
            raise AssertionError("verified view must provide hints")

    payload = {
        "selection_type": "hextech",
        "active": True,
        "visible": True,
        "source": {
            "session_id": "game-a",
            "game_instance_id": "game-a",
            "window_hwnd": 20,
            "selection_epoch": 1,
            "selection_revision": 1,
            "selection_window_active": True,
            "scene_state": "active",
        },
        "slots": [
            {"slot": index, "state": "ready", "augment_id": str(index), "name": str(index)}
            for index in range(3)
        ],
    }
    worker = module.OverlayDataPreparation(Source())
    worker._scope.resolve = lambda *_args, **_kwargs: SimpleNamespace(
        to_status=lambda: {"status": "ready"},
        semantic_key=lambda: ("stage", 1),
    )
    try:
        key = worker.request(
            payload,
            {"ok": True, "champion_id": "1"},
            completed=0,
            host_read_at=time.time(),
            viewport_size=(1920, 1080),
            display_mode="compact",
        )
        deadline = time.monotonic() + 2.0
        result = None
        while time.monotonic() < deadline:
            result = worker.poll(key)
            if result is not None and result.phase == "ready":
                break
            threading.Event().wait(0.005)
        assert result is not None and result.phase == "ready"
        assert result.model["synergies"][0]["display_summary"]["status"] == "ready"
        assert worker_threads and set(worker_threads) == {worker._thread.ident}
        assert threading.get_ident() not in worker_threads
    finally:
        worker.close()


class _RecordingCanvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.text_calls: list[dict] = []

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height

    def delete(self, *_args) -> None:
        return None

    def create_polygon(self, *_args, **_kwargs) -> None:
        return None

    def create_line(self, *_args, **_kwargs) -> None:
        return None

    def create_rectangle(self, *_args, **_kwargs) -> None:
        return None

    def create_text(self, *_args, **kwargs) -> None:
        self.text_calls.append(dict(kwargs))


def _canvas_model(content: str) -> dict:
    return {
        "stats": [
            {
                "slot": index,
                "state": "detecting",
                "name": "",
                "tier": "Gold",
                "stats_text": "识别中…",
                "status_code": "DETECTING",
                "winrate_text": "",
                "pickrate_text": "",
                "status_text": "识别中…",
                "synergy_status": "CONTEXT_MISSING",
            }
            for index in range(3)
        ],
        "synergies": [
            {
                "slot": 0,
                "augment_name": "升级：无尽之刃",
                "tier": "Gold",
                "hero_name": "测试英雄",
                "rating": "S",
                "tag": "联动",
                "content": content,
            }
        ],
    }


def test_canvas_replaces_unsafe_partial_text_and_records_complete_diagnostic() -> None:
    from hextech.interfaces.overlay.canvas_renderer import draw_overlay_frame

    raw = "只有在目标被控制2秒时才造成125点伤害，否则不会触发。" * 40
    canvas = _RecordingCanvas(2560, 1600)
    perf: dict = {}

    draw_overlay_frame(canvas, _canvas_model(raw), expanded=True, perf_sink=perf)

    visible_text = [str(call.get("text") or "") for call in canvas.text_calls]
    assert "内容较长，已保留完整原文待审" in visible_text
    assert not any("只有在目标" in text for text in visible_text)
    assert perf["proposed_synergy_slots"] == [0]
    assert perf["drawn_synergy_slots"] == []
    assert perf["panel_drawn_synergy_slots"] == [0]
    assert perf["fallback_drawn_synergy_slots"] == [0]
    assert perf["missing_synergy_slots"] == [
        {"slot": 0, "reason": "canvas_overflow_without_safe_summary"}
    ]
    audit = perf["synergy_render"][0]
    assert audit["panel_drawn"] is True
    assert audit["content_drawn"] is False
    assert audit["fallback_drawn"] is True
    assert audit["fallback_verified"] is False
    assert audit["bbox_available"] is False
    assert audit["bbox_verified"] is False
    assert audit["reason"] == "canvas_overflow_without_safe_summary"
    assert audit["original_char_count"] == len(raw)
    assert audit["truncated"] is False
    assert perf["display_layout"]["synergy_render"] == perf["synergy_render"]


def test_canvas_reports_every_suppressed_synergy_slot_with_reason() -> None:
    from hextech.interfaces.overlay.canvas_renderer import draw_overlay_frame

    canvas = _RecordingCanvas(640, 360)
    perf: dict = {}

    draw_overlay_frame(canvas, _canvas_model("完整短句。"), expanded=False, perf_sink=perf)

    assert perf["proposed_synergy_slots"] == [0]
    assert perf["drawn_synergy_slots"] == []
    assert perf["missing_synergy_slots"] == [
        {"slot": 0, "reason": "insufficient_vertical_space"}
    ]
