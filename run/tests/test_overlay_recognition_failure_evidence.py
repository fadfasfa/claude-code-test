"""识别失败 ROI 的隔离、脱敏、有界与去重合同。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

from PIL import Image


def _draft(slot_key: str, *, session_id: str, fingerprint: str = "slot-roi-ahash-v1:test"):
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceDraft, RoiImage

    return FailureEvidenceDraft(
        slot_key=slot_key,
        fingerprint=fingerprint,
        record={
            "session_id": session_id,
            "game_instance_id": f"game-{session_id}",
            "selection_epoch": 1,
            "slot_generation": 1,
            "slot_index": 0,
            "frame_id": 10,
            "failure_reason": "evidence_starved",
        },
        title_crop=RoiImage.from_image(Image.new("RGB", (90, 24), "white")),
        icon_crop=RoiImage.from_image(Image.new("RGB", (48, 48), "navy")),
        button_crop=RoiImage.from_image(Image.new("RGB", (120, 32), "gold")),
    )


def test_default_failure_root_is_current_isolated_var() -> None:
    from hextech.infrastructure.vision.failure_evidence import failure_inbox_root
    from hextech.modules.data.ports.paths import get_var_dir

    assert failure_inbox_root() == get_var_dir() / "recognition" / "failure-inbox"


def test_writer_dedupes_roi_and_never_authorizes_automatic_exemplar(tmp_path: Path) -> None:
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceWriter, load_failure_index

    first = FailureEvidenceWriter(tmp_path)
    first.start()
    assert first.submit(_draft("session-a:1:0:1", session_id="session-a"))
    assert first.wait_empty()
    first.close()

    second = FailureEvidenceWriter(tmp_path)
    second.start()
    assert second.submit(_draft("session-b:1:0:1", session_id="session-b"))
    assert second.wait_empty()
    second.close()

    index = load_failure_index(tmp_path)
    assert len(index["records"]) == 1
    assert set(index["slot_keys"]) == {"session-a:1:0:1", "session-b:1:0:1"}
    entry = next(iter(index["records"].values()))
    record = json.loads((tmp_path / entry["record_relative_path"]).read_text(encoding="utf-8"))
    assert record["occurrence_count"] == 2
    assert record["automatic_exemplar_eligible"] is False
    assert record["requires_manual_truth"] is True
    stored_hash = record.pop("record_sha256")
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert stored_hash == hashlib.sha256(canonical).hexdigest()
    assert len(list((tmp_path / "blobs").glob("*.png"))) == 3
    assert not any(path.name.lower().startswith("frame") for path in tmp_path.rglob("*.png"))


def test_collector_saves_only_title_icon_button_while_public_slot_stays_detecting(tmp_path: Path) -> None:
    from hextech.infrastructure.vision.failure_evidence import (
        FailureEvidenceCollector,
        FailureEvidenceWriter,
        load_failure_index,
    )

    writer = FailureEvidenceWriter(tmp_path)
    writer.start()
    collector = FailureEvidenceCollector(writer, pool_id="pool-test")
    frame = Image.new("RGB", (320, 200), "black")
    raw_slot = {
        "slot": 0,
        "diagnostic": "text_icon_disagree",
        "evidence_fingerprint": "slot-roi-ahash-v1:collector",
        "top_candidates": [{"augment_id": "candidate", "name": "候选", "confidence": 0.71}],
        "channels": {
            "text": {
                "margin": 0.01,
                "top_candidates": [{"augment_id": "candidate", "name": "候选", "confidence": 0.71}],
            }
        },
    }
    source = {
        "session_id": "collector-session",
        "game_instance_id": "collector-game",
        "selection_epoch": 3,
        "scene_present": True,
        "scene_state": "active",
        "scene_kind": "hextech",
        "layout_transform": {},
        "preset": "auto",
        "button_box": [100, 150, 220, 190],
        "capture_size": [320, 200],
        "frame_id": 12,
        "cursor_over_slots": [],
    }
    raw_event = {"source": source, "_raw_slots": [raw_slot, {}, {}]}
    event = {
        "selection_type": "hextech",
        "source": source,
        "slots": [
            {
                "slot": 0,
                "state": "detecting",
                "temporal_state": "evidence_starved",
                "rejection_reason": "text_icon_disagree",
            },
            {"slot": 1, "state": "ready"},
            {"slot": 2, "state": "ready"},
        ],
    }

    with mock.patch.object(
        FailureEvidenceCollector,
        "_boxes",
        return_value=((10, 10, 100, 34), (10, 40, 58, 88), (100, 150, 220, 190)),
    ):
        submitted = collector.observe(frame, raw_event, event, slot_generations=[1, 1, 1])
        assert collector.observe(frame, raw_event, event, slot_generations=[1, 1, 1]) == []

    assert submitted == ["collector-session:3:0:1"]
    assert event["slots"][0]["state"] == "detecting"
    assert writer.wait_empty()
    writer.close()
    record_entry = next(iter(load_failure_index(tmp_path)["records"].values()))
    record = json.loads((tmp_path / record_entry["record_relative_path"]).read_text(encoding="utf-8"))
    assert record["pool_id"] == "pool-test"
    assert record["failure_reason"] == "text_icon_disagree"
    assert set(record) >= {"title_roi", "icon_roi", "button_roi", "channel_diagnostics"}


def test_occurrences_keep_latest_twenty_and_preserve_total(tmp_path: Path) -> None:
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceWriter, load_failure_index

    for index in range(25):
        writer = FailureEvidenceWriter(tmp_path)
        assert writer.submit(_draft(f"session-{index}:1:0:1", session_id=f"session-{index}"))
        assert writer.wait_empty()
        writer.close()

    entry = next(iter(load_failure_index(tmp_path)["records"].values()))
    record = json.loads((tmp_path / entry["record_relative_path"]).read_text(encoding="utf-8"))
    assert record["occurrence_count"] == 25
    assert record["occurrence_total_count"] == 25
    assert len(record["occurrences"]) == 20
    assert record["occurrences"][0]["session_id"] == "session-5"
    assert len(load_failure_index(tmp_path)["slot_keys"]) == 20


def test_record_limit_drops_new_record_without_writer_side_deletion(tmp_path: Path) -> None:
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceWriter, load_failure_index

    writer = FailureEvidenceWriter(tmp_path, record_limit=1)
    assert writer.submit(_draft("old:1:0:1", session_id="old", fingerprint="fp:old"))
    assert writer.wait_empty()
    old_record = next(iter(load_failure_index(tmp_path)["records"]))
    assert writer.submit(_draft("new:1:0:1", session_id="new", fingerprint="fp:new"))
    assert writer.wait_empty()
    writer.close()

    index = load_failure_index(tmp_path)
    assert len(index["records"]) == 1
    assert old_record in index["records"]
    assert set(index["slot_keys"]) == {"old:1:0:1"}
    assert len(list((tmp_path / "blobs").glob("*.png"))) == 3
    assert writer.status()["dropped"] == 1
    assert writer.drain_journal_events()[0]["detail"] == "record_limit_reached"


def test_unique_queue_overflow_is_explicit_and_bounded(tmp_path: Path) -> None:
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceWriter

    writer = FailureEvidenceWriter(tmp_path, max_queue=1)
    with mock.patch.object(writer, "start"):
        assert writer.submit(_draft("session-a:1:0:1", session_id="session-a", fingerprint="fp:a"))
        assert not writer.submit(_draft("session-b:1:0:1", session_id="session-b", fingerprint="fp:b"))

    journal = writer.drain_journal_events()
    assert journal[0]["reason"] == "failure_evidence_write_failed"
    assert journal[0]["detail"] == "queue_full_unique_record"


def test_runner_closes_failure_writer_when_loop_raises() -> None:
    from hextech.infrastructure.vision import runner

    writer = mock.Mock()
    with (
        mock.patch.object(runner, "FailureEvidenceWriter", return_value=writer),
        mock.patch.object(runner, "_run_loop_impl", side_effect=RuntimeError("stop")),
    ):
        try:
            runner.run_loop(write_event=True)
        except RuntimeError as exc:
            assert str(exc) == "stop"
        else:
            raise AssertionError("run_loop 应透传识别循环异常")

    writer.close.assert_called_once_with(timeout=5.0)
