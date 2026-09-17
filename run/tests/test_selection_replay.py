import hashlib
import json

import pytest

from tooling.diagnostics.selection_replay import replay_group
from hextech.infrastructure.vision.selection_capture_cache import persist_selection_capture
from test_selection_capture_cache import build_draft


def saved(tmp_path):
    draft = build_draft()
    draft.frames[0].metadata.update(game_instance_id="real-game", frame_id=1, captured_at=100)
    persist_selection_capture(tmp_path, draft)
    group = tmp_path / draft.diagnostic_id
    truth = {"diagnostic_id": draft.diagnostic_id,
             "manifest_sha256": hashlib.sha256((group / "manifest.json").read_bytes()).hexdigest(),
             "frames": [{"frame_index": 0, "expected_slots": ["123", "456", None]}]}
    return group, truth


def test_unknown_and_wrong_are_not_dropped_from_replay(tmp_path):
    group, truth = saved(tmp_path)
    def recognize(frame):
        assert frame.info["hextech_roi_size"] != frame.size
        return {"slots": [{"state": "ready", "augment_id": "123"},
                          {"state": "detecting"}, {"state": "ready", "augment_id": "789"}]}
    result = replay_group(group, truth, recognize)
    assert result["evaluated_slots"] == 3
    assert result["wrong_ready"] == 1
    assert result["unknown_slots"] == 1
    assert not result["qualified"] and not result["temporal_acceptance"]


def test_truth_must_bind_exact_capture_and_cover_every_frame(tmp_path):
    group, truth = saved(tmp_path)
    truth["frames"] = []
    with pytest.raises(ValueError, match="all_saved_frames"):
        replay_group(group, truth, lambda _: {})
    truth["manifest_sha256"] = "bad"
    with pytest.raises(ValueError, match="binding"):
        replay_group(group, truth, lambda _: {})


def test_tampered_saved_pixels_rejected(tmp_path):
    group, truth = saved(tmp_path)
    manifest = json.loads((group / "manifest.json").read_text())
    (group / manifest["frames"][0]["file"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="hash"):
        replay_group(group, truth, lambda _: {})


def test_scene_only_replay_is_read_only_and_never_qualified(tmp_path):
    from PIL import Image
    from test_selection_capture_cache import buffer, event
    from tooling.diagnostics.selection_replay import replay_scene_group

    collector, _, drafts = buffer()
    raw = event()
    raw["source"].update(game_instance_id="game", frame_id=1,
                         layout_transform={"dx_ratio": 0, "dy_ratio": 0, "scale": 1})
    raw["timing"] = {"captured_at": 100}
    collector.capture(Image.new("RGB", (1920, 1080)), raw)
    collector.observe_result(raw)
    draft = drafts[-1]
    persist_selection_capture(tmp_path, draft)
    group = tmp_path / draft.diagnostic_id
    before = {p.name: p.read_bytes() for p in group.iterdir()}
    report = replay_scene_group(group)
    assert not report["qualified"] and not report["temporal_acceptance"]
    assert not report["frames"][0]["replayed_scene"]["present"]
    assert before == {p.name: p.read_bytes() for p in group.iterdir()}
    manifest = json.loads((group / "manifest.json").read_text())
    manifest["frames"][0]["selection_box"] = [0, 0, 20, 20]
    (group / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="scene_button_search_not_captured"):
        replay_scene_group(group)


def test_candidate_export_is_explicit_immutable_and_not_promoted(tmp_path, monkeypatch):
    from PIL import Image
    from test_selection_capture_cache import buffer, event
    from hextech.modules.data.ports import paths
    from tooling.diagnostics.selection_replay import export_labelled_candidate

    collector, _, drafts = buffer()
    raw = event()
    raw["source"].update(game_instance_id="game", frame_id=1,
                         layout_transform={"dx_ratio": 0, "dy_ratio": 0, "scale": 1})
    raw["timing"] = {"captured_at": 100}
    collector.capture(Image.new("RGB", (1920, 1080)), raw)
    collector.observe_result(raw)
    draft = drafts[-1]
    cache = tmp_path / "cache"
    persist_selection_capture(cache, draft)
    group = cache / draft.diagnostic_id
    truth = {"diagnostic_id": draft.diagnostic_id,
             "manifest_sha256": hashlib.sha256((group / "manifest.json").read_bytes()).hexdigest(),
             "frames": [{"frame_index": 0, "expected_slots": ["123", None, None]}]}
    monkeypatch.setattr(paths, "get_var_dir", lambda: tmp_path)
    output = tmp_path / "recognition/corpus/samples/candidate-one"
    result = export_labelled_candidate(group, truth, output)
    assert result["state"] == "candidate_requires_holdout_review"
    assert not result["automatic_exemplar_eligible"]
    assert (output / "candidate.json").is_file()
    with pytest.raises(ValueError, match="new_corpus_directory"):
        export_labelled_candidate(group, truth, output)
