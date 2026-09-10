"""生产 OCR 异步完成证据的原帧回流回归。"""

from __future__ import annotations

import time
import math
from types import SimpleNamespace

from PIL import Image

from hextech.infrastructure.vision.ocr_shadow import OcrShadowRuntime
from hextech.infrastructure.vision.state import SelectionTracker


def _templates() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(augment_id="1225", name="双刀流"),
        SimpleNamespace(augment_id="1349", name="终极唤醒"),
    ]


class _ExactRecognizer:
    model_sha256 = "test-model"

    def recognize(self, images):
        return [("双刀流", 0.99)] * len(images)


def _raw_slot(index: int, frame_id: int, *, ocr: bool = False) -> dict:
    slot = {
        "slot": index,
        "evidence_fingerprint": f"fp-{index}-{frame_id}",
        "ocr_shadow": {"state": "pending", "input_sha256": f"{frame_id:064x}"},
    }
    if ocr:
        slot["ocr_production"] = _completed(frame_id, slot_index=index)["evidence"]
    return slot


def _completed(
    frame_id: int,
    *,
    slot_index: int = 0,
    captured_at: float | None = None,
    template_id: str = "",
    template_grade: str = "",
) -> dict:
    return {
        "evidence": {
            "schema_version": 1,
            "state": "admitted",
            "session_id": "session-completed",
            "selection_epoch": 1,
            "slot_index": slot_index,
            "slot_generation": 1,
            "rgb_sha256": f"{frame_id:064x}",
            "perceptual_fingerprint": f"fp-{slot_index}-{frame_id}",
            "captured_frame_id": frame_id,
            "captured_at": float(captured_at if captured_at is not None else 100.0 + frame_id),
            "canonical_id": "1225",
            "name": "双刀流",
            "confidence": 0.99,
            "match_rule": "exact",
            "acceptance_rule": "ocr_exact_fallback",
        },
        "template_candidate_id": template_id,
        "template_evidence_grade": template_grade,
    }


def _event(
    frame_id: int,
    *,
    slots: list[dict] | None = None,
    completed: list[dict] | None = None,
    outcomes: list[str] | None = None,
) -> dict:
    payload = {
        "active": True,
        "selection_type": "hextech",
        "source": {
            "session_id": "session-completed",
            "scene_present": True,
            "scene_kind": "hextech",
            "selection_window_active": True,
            "selection_button_present": True,
            "frame_id": frame_id,
        },
        "_raw_slots": slots
        if slots is not None
        else [_raw_slot(index, frame_id) for index in range(3)],
        "timing": {
            "captured_at": 100.0 + frame_id,
            "recognition_completed_at": 100.01 + frame_id,
        },
    }
    if completed is not None:
        payload["_completed_ocr_evidence"] = completed
        payload["_completed_ocr_outcomes"] = outcomes if outcomes is not None else []
    return payload


def test_worker_completion_is_delivered_with_original_observation_without_same_sha_reappearing() -> None:
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: _ExactRecognizer(),
    )
    captured_at = time.time()
    try:
        runtime.set_production_context(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            captured_frame_id=41,
            captured_at=captured_at,
        )
        source_frame = {"slot": 0, "evidence_fingerprint": "fp-original"}
        runtime.observe([Image.new("RGB", (160, 32), "black")], [source_frame])
        assert runtime.wait_until_idle()

        completed = runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            observed_at=captured_at + 0.2,
            scene_open=True,
        )

        assert len(completed) == 1
        assert completed[0]["evidence"]["captured_frame_id"] == 41
        assert completed[0]["evidence"]["captured_at"] == captured_at
        assert completed[0]["evidence"]["perceptual_fingerprint"] == "fp-original"
        assert completed[0]["evidence"]["canonical_id"] == "1225"
        tracker = SelectionTracker(scene_enter_frames=1)
        tracker.update(
            _event(41, slots=[source_frame, _raw_slot(1, 41), _raw_slot(2, 41)])
        )
        outcomes: list[str] = []
        tracker.update(_event(42, completed=completed, outcomes=outcomes))
        assert outcomes == ["upgraded"]
        assert runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=7,
            slot_generations=(3,),
            observed_at=captured_at + 0.3,
            scene_open=True,
        ) == []
    finally:
        runtime.close()


def test_completed_result_is_rejected_for_late_future_generation_or_closed_scene() -> None:
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: _ExactRecognizer(),
    )

    def submit(frame_id: int, captured_at: float) -> None:
        runtime.set_production_context(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            captured_frame_id=frame_id,
            captured_at=captured_at,
        )
        image = Image.new("RGB", (160, 32), (frame_id, frame_id, frame_id))
        image.putpixel((frame_id % 159, 0), (255, 255, 255))
        runtime.observe([image], [{"slot": 0, "evidence_fingerprint": f"fp-{frame_id}"}])
        assert runtime.wait_until_idle()

    now = time.time()
    try:
        submit(1, now - 6.01)
        assert runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            observed_at=now,
            scene_open=True,
        ) == []
        submit(2, now + 0.5)
        assert runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            observed_at=now,
            scene_open=True,
        ) == []
        submit(3, now)
        assert runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(2,),
            observed_at=now + 0.1,
            scene_open=True,
        ) == []
        submit(4, now)
        assert runtime.drain_completed_evidence(
            session_id="session-completed",
            selection_epoch=1,
            slot_generations=(1,),
            observed_at=now + 0.1,
            scene_open=False,
        ) == []

        status = runtime.status()
        assert status["completed_rejected_late"] == 1
        assert status["completed_rejected_future"] == 1
        assert status["completed_rejected_context"] == 1
        assert status["completed_rejected_scene"] == 1

        for frame_id, invalid_time in ((5, math.nan), (6, math.inf)):
            submit(frame_id, invalid_time)
            assert runtime.drain_completed_evidence(
                session_id="session-completed",
                selection_epoch=1,
                slot_generations=(1,),
                observed_at=now + 0.1,
                scene_open=True,
            ) == []
        assert runtime.status()["completed_rejected_clock"] == 2
    finally:
        runtime.close()


def test_completed_source_frame_upgrades_in_place_and_third_independent_capture_becomes_ready() -> None:
    tracker = SelectionTracker(scene_enter_frames=1)
    tracker.update(_event(1))
    tracker.update(_event(2, slots=[_raw_slot(0, 2, ocr=True), _raw_slot(1, 2), _raw_slot(2, 2)]))
    outcomes: list[str] = []

    ready = tracker.update(
        _event(
            3,
            slots=[_raw_slot(0, 3, ocr=True), _raw_slot(1, 3), _raw_slot(2, 3)],
            completed=[_completed(1)],
            outcomes=outcomes,
        )
    )

    assert outcomes == ["upgraded"]
    assert ready["slots"][0]["state"] == "ready"
    assert ready["slots"][0]["augment_id"] == "1225"
    assert ready["slots"][0]["observed_frames"] == 3
    assert [item.frame_id for item in tracker.slots[0].observations] == [1, 2, 3]


def test_completed_source_frame_counts_once_and_single_capture_never_authorizes_ready() -> None:
    tracker = SelectionTracker(scene_enter_frames=1)
    tracker.update(_event(1))
    first_outcomes: list[str] = []
    second = tracker.update(_event(2, completed=[_completed(1)], outcomes=first_outcomes))
    repeated_outcomes: list[str] = []
    third = tracker.update(_event(3, completed=[_completed(1)], outcomes=repeated_outcomes))

    assert first_outcomes == ["upgraded"]
    assert repeated_outcomes == ["duplicate"]
    assert second["slots"][0]["state"] == "detecting"
    assert third["slots"][0]["state"] == "detecting"
    assert sum(
        item.candidate is not None and item.candidate.rule == "ocr_exact_fallback"
        for item in tracker.slots[0].observations
    ) == 1


def test_completed_source_frame_outside_recent_five_and_original_strong_conflict_are_rejected() -> None:
    expired = SelectionTracker(scene_enter_frames=1)
    for frame_id in range(1, 7):
        expired.update(_event(frame_id))
    expired_outcomes: list[str] = []
    expired_event = expired.update(_event(7, completed=[_completed(1)], outcomes=expired_outcomes))
    assert expired_outcomes == ["evidence_window_expired"]
    assert expired_event["slots"][0]["state"] == "detecting"

    conflict = SelectionTracker(scene_enter_frames=1)
    from support.vision_events import ready_slot

    conflict.update(
        _event(
            1,
            slots=[
                {
                    **ready_slot(0, "1349", "终极唤醒"),
                    "evidence_fingerprint": "fp-0-1",
                    "ocr_shadow": {"state": "pending", "input_sha256": f"{1:064x}"},
                },
                _raw_slot(1, 1),
                _raw_slot(2, 1),
            ],
        )
    )
    conflict_outcomes: list[str] = []
    conflict_event = conflict.update(
        _event(
            2,
            completed=[
                _completed(1, template_id="1349", template_grade="strong")
            ],
            outcomes=conflict_outcomes,
        )
    )
    assert conflict_outcomes == ["ocr_template_conflict"]
    assert conflict_event["slots"][0]["state"] == "detecting"


def test_same_identity_from_different_slots_and_batches_never_accumulates_two_ready_tracks() -> None:
    tracker = SelectionTracker(scene_enter_frames=1)
    tracker.update(_event(1))
    first_outcomes: list[str] = []
    tracker.update(_event(2, completed=[_completed(1, slot_index=0)], outcomes=first_outcomes))
    second_outcomes: list[str] = []
    tracker.update(
        _event(3, completed=[_completed(2, slot_index=1)], outcomes=second_outcomes)
    )

    assert first_outcomes == ["upgraded"]
    assert second_outcomes == ["cross_slot_identity_conflict"]
    assert all(slot.stable_slot is None for slot in tracker.slots)
    assert not any(
        observation.candidate is not None
        and observation.candidate.rule == "ocr_exact_fallback"
        and observation.candidate.identity == "双刀流"
        for slot in tracker.slots
        for observation in slot.observations
    )


def test_three_delayed_independent_votes_confirm_without_turning_current_miss_into_vote():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in range(1, 4):
        tracker.update(_event(frame))
    outcomes = []
    event = tracker.update(_event(4, completed=[_completed(i) for i in range(1, 4)], outcomes=outcomes))
    assert outcomes == ["upgraded"] * 3
    assert event["slots"][0]["state"] == "ready"
    assert event["slots"][0]["evidence_hits"] == 3
    assert tracker.slots[0].observations[-1].frame_id == 4
    assert tracker.slots[0].observations[-1].candidate is None


def test_current_cached_ocr_cannot_duplicate_identity_from_completed_votes():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in range(1, 4):
        tracker.update(_event(frame))
    event = tracker.update(_event(4, completed=[_completed(i) for i in range(1, 4)],
        slots=[_raw_slot(0, 4), _raw_slot(1, 4, ocr=True), _raw_slot(2, 4)]))
    assert all(slot["state"] == "detecting" for slot in event["slots"])


def test_completed_original_input_sha_and_ended_epoch_are_not_reused():
    tracker = SelectionTracker(scene_enter_frames=1)
    tracker.update(_event(1))
    wrong = _completed(1)
    wrong["evidence"]["rgb_sha256"] = "f" * 64
    outcomes = []
    tracker.update(_event(2, completed=[wrong], outcomes=outcomes))
    assert outcomes == ["ocr_result_stale"]
    ended = _event(3, completed=[_completed(1)])
    ended["source"]["selection_confirmed"] = True
    assert tracker.update(ended)["active"] is False
    assert all(not slot.observations for slot in tracker.slots)


def test_shutdown_discards_unconsumed_completed_evidence():
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: _ExactRecognizer(),
    )
    runtime.set_production_context(
        session_id="session-completed",
        selection_epoch=1,
        slot_generations=(1,),
        captured_frame_id=1,
        captured_at=time.time(),
    )
    runtime.observe([Image.new("RGB", (160, 32), "black")], [_raw_slot(0, 1)])
    assert runtime.wait_until_idle()
    assert runtime.status()["completed_queue_depth"] == 1

    runtime.close()

    status = runtime.status()
    assert status["completed_queue_depth"] == 0
    assert status["completed_dropped_shutdown"] == 1


def test_shutdown_during_inference_drops_completion_and_rejects_later_observe():
    import threading
    entered, release = threading.Event(), threading.Event()
    class SlowRecognizer(_ExactRecognizer):
        def recognize(self, images):
            entered.set()
            assert release.wait(2)
            return super().recognize(images)
    runtime = OcrShadowRuntime(_templates(), mode="admit", recognizer_factory=lambda _: SlowRecognizer())
    try:
        now = time.time()
        runtime.set_production_context(session_id="s", selection_epoch=1, slot_generations=(1,),
            captured_frame_id=1, captured_at=now)
        runtime.observe([Image.new("RGB", (100, 32), "black")], [{"evidence_fingerprint": "fp"}])
        assert entered.wait(1)
        runtime.close(timeout=0)
        release.set()
        runtime.close()
        assert runtime.drain_completed_evidence(session_id="s", selection_epoch=1, slot_generations=(1,),
            observed_at=now+.5, scene_open=True) == []
        runtime.observe([Image.new("RGB", (100, 32), "white")], [{"evidence_fingerprint": "new"}])
        assert runtime.status()["completed_queue_depth"] == 0
        assert runtime.status()["production_submitted"] == 1
    finally:
        release.set()
        runtime.close()


# 独立审查反例：只追加测试，不放宽生产门；review 前缀便于主线程单独运行。
def _review_event(frame_id: int, **kwargs) -> dict:
    """所有合成观察相隔 50 ms，证据采集时间与事件时间使用同一时钟。"""
    event = _event(frame_id, **kwargs)
    captured_at = 100.0 + frame_id * 0.05
    event["timing"] = {
        "captured_at": captured_at,
        "recognition_completed_at": captured_at + 0.001,
    }
    for slot in event["_raw_slots"]:
        if "ocr_production" in slot:
            slot["ocr_production"]["captured_at"] = captured_at
    for item in event.get("_completed_ocr_evidence", []):
        evidence = item["evidence"]
        evidence["captured_at"] = 100.0 + evidence["captured_frame_id"] * 0.05
    return event


def _review_template_slot(frame_id: int, *, medium: bool = False) -> dict:
    """strong B 或 medium A；保留每帧 SHA 和 fingerprint 绑定。"""
    from support.vision_events import medium_slot, ready_slot

    template = medium_slot(0, "1225", "双刀流") if medium else ready_slot(0, "1349", "终极唤醒")
    return {**_raw_slot(0, frame_id), **template}


def test_review_pending_reroll_invalidates_old_generation_completed_vote() -> None:
    """F1 pending、F2/F3 exact A；未 READY 时重随不能让 F1 回流复活旧 A。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    tracker.update(_review_event(1))
    for frame_id in (2, 3):
        event = tracker.update(_review_event(frame_id, slots=[_raw_slot(0, frame_id, ocr=True)]))
        assert event["slots"][0]["state"] == "detecting"
    old_generation = tracker.slots[0].slot_generation
    click = _review_event(4)
    click["source"].update(
        selection_click=True,
        transition_kind="reroll",
        transition_source="async_mouse_down",
        transition_slot=0,
    )
    tracker.update(click)
    outcomes: list[str] = []
    event = tracker.update(_review_event(5, completed=[_completed(1)], outcomes=outcomes))

    assert event["slots"][0]["state"] == "detecting", (event["slots"][0], outcomes)
    assert tracker.slots[0].slot_generation > old_generation
    assert "upgraded" not in outcomes


def test_review_current_strong_conflict_blocks_delayed_ready() -> None:
    """当前 strong B 与 cached exact A 冲突，历史三票 A 不得绕过该硬拒绝。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in (1, 2, 3):
        tracker.update(_review_event(frame_id))
    current = _review_template_slot(4)
    current["ocr_production"] = _completed(4)["evidence"]
    outcomes: list[str] = []
    event = tracker.update(
        _review_event(4, slots=[current], completed=[_completed(i) for i in (1, 2, 3)], outcomes=outcomes)
    )

    assert event["slots"][0]["state"] == "detecting", (event["slots"][0], outcomes)
    assert event["slots"][0]["rejection_reason"] == "ocr_template_conflict"


def test_review_stable_replacement_uses_last_five_raw_observations() -> None:
    """B 已稳定；A 只出现在 F3/F6/F9，最近五帧不足三票，不得替换。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in (1, 2):
        tracker.update(_review_event(frame_id, slots=[_review_template_slot(frame_id)]))
    assert tracker.slots[0].stable_slot["augment_id"] == "1349"

    for frame_id in range(3, 10):
        slot = _raw_slot(0, frame_id, ocr=True) if frame_id in (3, 6, 9) else _review_template_slot(frame_id)
        event = tracker.update(_review_event(frame_id, slots=[slot]))
        assert event["slots"][0]["augment_id"] == "1349", (frame_id, event["slots"][0])


def test_review_stable_pending_frames_accept_delayed_replacement() -> None:
    """旧 strong 已离开五帧窗口；三个真实 pending 帧回流应原子纠正 last-good。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in (1, 2):
        tracker.update(_review_event(frame_id, slots=[_review_template_slot(frame_id)]))
    assert tracker.slots[0].stable_slot["augment_id"] == "1349"
    old_generation = tracker.slots[0].slot_generation
    for frame_id in range(3, 8):
        event = tracker.update(_review_event(frame_id))
        assert event["slots"][0]["augment_id"] == "1349"
    outcomes: list[str] = []
    event = tracker.update(
        _review_event(8, completed=[_completed(i) for i in (5, 6, 7)], outcomes=outcomes)
    )

    assert event["slots"][0]["augment_id"] == "1225", (event["slots"][0], outcomes)
    assert event["slots"][0]["state"] == "ready"
    assert event["slots"][0]["slot_generation"] > old_generation
    assert "evidence_window_expired" not in outcomes


def test_review_initial_ocr_exact_does_not_count_medium_template_votes() -> None:
    """两票 medium A 加一票 exact A，不是三个独立 OCR exact 观察。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in (1, 2):
        event = tracker.update(_review_event(frame_id, slots=[_review_template_slot(frame_id, medium=True)]))
        assert event["slots"][0]["state"] == "detecting"
    event = tracker.update(_review_event(3, slots=[_raw_slot(0, 3, ocr=True)]))

    assert event["slots"][0]["state"] == "detecting", event["slots"][0]


def test_review_worker_completion_before_same_tick_tracker_update_is_not_lost() -> None:
    """复现 runner 的 drain-before-update 顺序，不重播相同输入 SHA。"""
    from hextech.infrastructure.vision.runner_helpers import attach_completed_ocr_evidence

    tracker = SelectionTracker(scene_enter_frames=1)
    runtime = OcrShadowRuntime(
        _templates(), mode="admit", min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: _ExactRecognizer(),
    )
    input_hashes: set[str] = set()
    all_outcomes: list[str] = []
    try:
        for frame_id in (1, 2, 3):
            captured_at = time.time()
            runtime.set_production_context(
                session_id="session-completed", selection_epoch=1,
                slot_generations=(1,), captured_frame_id=frame_id, captured_at=captured_at,
            )
            image = Image.new("RGB", (160, 32), (frame_id * 30,) * 3)
            image.putpixel((frame_id, 0), (255, 0, 0))
            slot = _raw_slot(0, frame_id)
            runtime.observe([image], [slot])
            input_hashes.add(slot["ocr_shadow"]["input_sha256"])
            # 确定性安排：完成发生在 runner 本 tick drain 前，不依赖竞速或 sleep。
            assert runtime.wait_until_idle()
            raw = _event(frame_id, slots=[slot])
            observed_at = time.time()
            raw["timing"] = {"captured_at": captured_at, "recognition_completed_at": observed_at}
            outcomes = attach_completed_ocr_evidence(
                raw, runtime, tracker, session_id="session-completed",
                selection_epoch=1, observed_at=observed_at,
            )
            event = tracker.update(raw)
            all_outcomes.extend(outcomes)
            if frame_id < 3:
                assert event["slots"][0]["state"] == "detecting"

        assert len(input_hashes) == 3
        assert event["slots"][0]["state"] == "ready", (event["slots"][0], all_outcomes)
        assert event["slots"][0]["augment_id"] == "1225"
        assert "evidence_window_expired" not in all_outcomes
    finally:
        runtime.close()


def test_review_completed_exact_upgrades_same_identity_medium_observation() -> None:
    """禁止混票不等于丢掉真实 exact：同帧同身份 medium 必须可升级为 OCR 票。"""
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in range(1, 5):
        kwargs = {"slots": [_review_template_slot(frame_id, medium=True)]} if frame_id in (1, 3) else {}
        event = tracker.update(_review_event(frame_id, **kwargs))
        assert event["slots"][0]["state"] == "detecting"
    outcomes: list[str] = []
    event = tracker.update(
        _review_event(5, completed=[_completed(i) for i in (1, 3, 5)], outcomes=outcomes)
    )

    assert event["slots"][0]["state"] == "ready", (event["slots"][0], outcomes)
    assert event["slots"][0]["augment_id"] == "1225"
    assert event["slots"][0]["acceptance_rule"] == "ocr_exact_fallback"
    assert sum(
        item.candidate is not None and item.candidate.rule == "ocr_exact_fallback"
        for item in tracker.slots[0].observations
    ) == 3
