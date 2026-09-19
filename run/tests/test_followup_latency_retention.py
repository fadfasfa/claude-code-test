"""Regression evidence for reroll priority and input-to-presentation timing ownership."""
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest

from hextech.infrastructure.vision import selection_capture_cache as capture
from hextech.infrastructure.vision import selection_capture_persistence as persistence
from hextech.interfaces.overlay.host_input import HostInputObserver
from hextech.interfaces.overlay.host_presentation import mark_canvas_drawn, presentation_status
from test_selection_capture_cache import build_draft


def ready_reroll(*, elapsed=80, reason="", origin=""):
    draft = build_draft()
    metadata = {**draft.frames[0].metadata, "raw_scene_evidence": {"scene_state": "active"},
                "final_classification": {"scene_state": "active", "reason": reason,
                    "slots": [{"state": "ready"}] * 3, "full_ready_elapsed_ms": elapsed}}
    frame = replace(draft.frames[0], metadata=metadata)
    return replace(draft, frames=(frame,), first_id=0, clear_id=0, last_id=0,
                   terminal={"reason": "slot_generation_changed", "group_origin": origin})


def test_normal_reroll_does_not_latch_anomaly_but_real_failures_do():
    normal = ready_reroll()
    assert normal.retention_class == "success"
    pending = replace(normal, frames=(replace(normal.frames[0], metadata={}),),
        terminal={"group_origin": "slot_generation_changed", "capture_reason": "initial_snapshot"})
    assert pending.retention_class == "weak"
    merged = capture.coalesce_selection_drafts(pending, normal)
    assert merged.retention_class == "success"
    failed = replace(ready_reroll(reason="evidence_starved"), diagnostic_id=normal.diagnostic_id)
    assert capture.coalesce_selection_drafts(failed, normal).retention_class == "anomaly"
    assert ready_reroll(elapsed=1000).retention_class == "anomaly"


def test_old_sparse_anomaly_cannot_be_downgraded_from_final_ready(tmp_path):
    old = ready_reroll()
    persistence.persist_selection_capture(tmp_path, old)
    path = tmp_path / old.diagnostic_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["retention_class"] = "anomaly"  # exact old build shape
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    assert persistence._manifest_retention_priority(manifest) == 300
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="budget_exhausted"):
        persistence.persist_selection_capture(tmp_path, replace(old, diagnostic_id=uuid4().hex), group_limit=1)
    assert path.read_bytes() == before


def test_real_anomaly_floor_survives_persistence_and_capacity_competition(tmp_path):
    normal = ready_reroll()
    failure = replace(ready_reroll(reason="evidence_starved"), diagnostic_id=normal.diagnostic_id)
    merged = capture.coalesce_selection_drafts(failure, normal)
    assert merged.retention_floor == "anomaly"
    persistence.persist_selection_capture(tmp_path, merged, group_limit=1)
    path = tmp_path / merged.diagnostic_id / "manifest.json"
    before = path.read_bytes()
    assert persistence._manifest_retention_priority(json.loads(before)) == 300
    with pytest.raises(ValueError, match="budget_exhausted"):
        persistence.persist_selection_capture(tmp_path, replace(normal, diagnostic_id=uuid4().hex), group_limit=1)
    assert path.read_bytes() == before


def test_slow_manual_unknown_and_incomplete_old_records_stay_protected(tmp_path):
    draft = ready_reroll(elapsed=1000)
    persistence.persist_selection_capture(tmp_path, draft)
    path = tmp_path / draft.diagnostic_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert persistence._manifest_retention_priority(manifest) == 300
    manifest["frames"][0]["final_classification"]["full_ready_elapsed_ms"] = 80
    for change in ({"labels": ["truth"]}, {"manual": True}, {"unknown": True},
                   {"frames": [{"final_classification": {}}]}):
        assert persistence._manifest_retention_priority({**manifest, **change}) == 300


def test_first_event_read_is_stable_while_read_freshness_advances():
    observer = HostInputObserver(SimpleNamespace(), now=lambda: 1.1)
    event = {"build_id": "build", "timing": {"event_written_at": .9},
             "source": {"selection_epoch": 1, "selection_revision": 1}}
    def publish(start, end):
        observer._publish_event(event, identity=("game", 123), event_read_started_at=start,
                                event_read_completed_at=end, observed_at=end)
        return observer.snapshot()
    first = publish(1, 1.02)
    repeated = publish(1.07, 1.08)
    assert repeated.first_event_read_completed_at == first.first_event_read_completed_at == 1.02
    assert repeated.host_read_at == repeated.observed_at == 1.08
    observer._publish_context({"ok": True, "champion_id": "38"}, identity=("game", 123),
        request_sequence=1, requested_at=1.02, read_started_at=1.02, read_completed_at=1.09, error="")
    contextual = observer.snapshot()
    assert contextual.first_event_read_completed_at == 1.02
    assert contextual.event_read_completed_at == 1.08
    event["timing"]["event_written_at"] = .95
    newer = publish(1.09, 1.10)
    assert newer.first_event_read_completed_at == 1.10


def test_prepared_input_timing_not_replaced_by_later_tick():
    original = {"host_read_at": 1.02, "event_read_started_at": 1.0,
                "event_read_completed_at": 1.02, "first_event_read_completed_at": 1.02,
                "context_read_completed_at": 1.025, "preparation_completed_at": 1.04}
    visibility = {"host_read_at": 1.08, "event_read_started_at": 1.07,
                  "event_read_completed_at": 1.08, "context_read_completed_at": 1.085,
                  "draw_started_at": 1.09, "render_event_input_timing": original}
    mark_canvas_drawn(visibility, completed_at=1.1)
    bound = presentation_status(visibility)["bound_timing"]
    for key, value in original.items():
        assert bound[key] == value
    assert bound["draw_started_at"] == 1.09 and bound["draw_completed_at"] == 1.1
    assert "render_event_input_timing" not in visibility


def test_real_file_lock_contention_never_flushes_into_another_owner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from hextech.infrastructure.persistence.file_lock import InterProcessFileLock

    path = tmp_path / "shared.lock"
    barrier = threading.Barrier(4)
    def contender():
        barrier.wait()
        successes = 0
        for _ in range(100):
            lock = InterProcessFileLock(path)
            if lock.acquire():
                assert lock.acquired
                successes += 1
                lock.release()
            assert not lock.acquired
        return successes
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(lambda _: contender(), range(4))) > 0
    final = InterProcessFileLock(path)
    assert final.acquire()
    final.release()
    assert json.loads(path.read_text())["pid"] > 0
