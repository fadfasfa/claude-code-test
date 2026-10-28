from dataclasses import replace
from datetime import datetime, timezone

from hextech.infrastructure.persistence.retention import apply_retention, apply_cohort_retention, protected_references
from hextech.modules.data.ports.atomic import atomic_write_json
from support.v4_release import make_release


def test_retention_protects_current_previous_and_all_source_assets(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    active = protected_references(tmp_path)
    assert manifest.generation_id in active["generations"]
    before = {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "sources").rglob("*") if p.is_file()}
    result = apply_retention(tmp_path, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert result["disposition"] == "completed"
    assert (tmp_path / "snapshots/generations" / manifest.generation_id).exists()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "sources").rglob("*") if p.is_file()} == before


def test_only_old_unreferenced_exact_v4_manifests_are_owned(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    root = tmp_path / "snapshots/generations"
    old = replace(manifest, generation_id="owned-old", created_at="2000-01-01T00:00:00+00:00")
    atomic_write_json(root / old.generation_id / "manifest.json", old.to_dict())
    unknown = replace(old, generation_id="with-private-note")
    atomic_write_json(root / unknown.generation_id / "manifest.json", unknown.to_dict())
    (root / unknown.generation_id / "manual.txt").write_text("protected")
    atomic_write_json(root / "legacy" / "manifest.json", {"schema_version": 3})
    result = apply_retention(tmp_path, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert result["removed_generations"] == 1
    assert not (root / old.generation_id).exists()
    assert (root / unknown.generation_id / "manual.txt").read_text() == "protected"
    assert (root / "legacy").exists()


def test_active_missing_or_corrupt_head_stops_retention(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    head = tmp_path / "snapshots/current.v4.json"
    head.write_bytes(b"bad")
    assert apply_retention(tmp_path)["disposition"] == "skipped_unverified"
    assert apply_cohort_retention(tmp_path, workers_idle=False)["disposition"] == "skipped_workers_active"
