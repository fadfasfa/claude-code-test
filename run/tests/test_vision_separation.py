"""验证识别分离度离线工具的统计口径。"""

from __future__ import annotations

import json
from pathlib import Path

from tooling.diagnostics import vision_separation


def _entry(
    *,
    epoch: int = 5,
    seq: int,
    text_name: str,
    margin: float,
    confidence: float = 0.8,
    observation_kind: str = "recognition",
    capture_status: str = "captured",
) -> dict:
    channel = {"augment_id": "", "name": text_name, "confidence": confidence, "margin": margin}
    return {
        "session_id": "session-a",
        "selection_epoch": epoch,
        "observation_seq": seq,
        "observation_kind": observation_kind,
        "capture_status": capture_status,
        "slots": [
            {
                "slot": 0,
                "name": "真值卡",
                "text": dict(channel),
                "text_alt": dict(channel),
                "icon": {"augment_id": "", "name": "", "confidence": None, "margin": None},
            },
            {"slot": 1, "name": "", "text": {}, "text_alt": {}, "icon": {}},
            {"slot": 2, "name": "", "text": {}, "text_alt": {}, "icon": {}},
        ],
    }


def _write_timeline(tmp_path: Path, entries: list[dict]) -> Path:
    timeline_dir = tmp_path / "overlay_vision_timelines"
    timeline_dir.mkdir(parents=True)
    target = timeline_dir / "selection-abc123-e0005.jsonl"
    target.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return timeline_dir


def test_separation_counts_hits_and_misses_per_channel(tmp_path: Path) -> None:
    entries = [
        _entry(seq=1, text_name="真值卡", margin=0.05),
        _entry(seq=2, text_name="真值卡", margin=0.04),
        _entry(seq=3, text_name="错误卡", margin=0.001),
        # 暂停探针没有截图，不得进入统计。
        _entry(seq=4, text_name="错误卡", margin=0.5, observation_kind="visibility_probe", capture_status="not_captured"),
    ]
    timeline_dir = _write_timeline(tmp_path, entries)
    loaded = vision_separation._load_timeline_entries(timeline_dir)
    truth = [{"session_id": "session-a", "selection_epoch": 5, "expected_slots": ["真值卡", "", ""]}]

    summary = vision_separation.analyze_separation(loaded, truth)

    text_report = summary["channels"]["text"]
    assert summary["matched_epochs"] == 1
    assert summary["unmatched_epochs"] == []
    assert text_report["observations"] == 3
    assert text_report["top1_hit_rate"] == round(2 / 3, 4)
    assert text_report["hit_margin"]["count"] == 2
    assert text_report["miss_margin"]["count"] == 1
    assert text_report["margin_mean_gap"] > 0
    # icon 通道 top1 为空，不产生 observation。
    assert summary["channels"]["icon"]["observations"] == 0


def test_unmatched_truth_epochs_are_reported_not_silently_dropped(tmp_path: Path) -> None:
    timeline_dir = _write_timeline(tmp_path, [_entry(seq=1, text_name="真值卡", margin=0.05)])
    loaded = vision_separation._load_timeline_entries(timeline_dir)
    truth = [
        {"session_id": "session-a", "selection_epoch": 5, "expected_slots": ["真值卡", "", ""]},
        {"session_id": "session-missing", "selection_epoch": 9, "expected_slots": ["某卡", "", ""]},
    ]

    summary = vision_separation.analyze_separation(loaded, truth)

    assert summary["truth_epochs"] == 2
    assert summary["matched_epochs"] == 1
    assert summary["unmatched_epochs"] == ["session-missing#e9"]


def test_list_labelable_epochs_surfaces_final_slot_names(tmp_path: Path) -> None:
    timeline_dir = _write_timeline(
        tmp_path,
        [
            _entry(seq=1, text_name="真值卡", margin=0.05),
            _entry(seq=2, text_name="真值卡", margin=0.04),
        ],
    )
    loaded = vision_separation._load_timeline_entries(timeline_dir)

    rows = vision_separation.list_labelable_epochs(loaded)

    assert rows == [
        {
            "session_id": "session-a",
            "selection_epoch": 5,
            "recognition_observations": 2,
            "final_slot_names": ["真值卡", "", ""],
        }
    ]
