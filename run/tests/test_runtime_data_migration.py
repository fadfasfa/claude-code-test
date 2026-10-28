"""Current seed installs only after full immutable closure validation."""
from pathlib import Path
import pytest
from support.v4_release import make_release
from hextech.infrastructure.persistence import release_seed
from hextech.modules.data.generation import DataSnapshotClient


def test_verified_generation_is_seeded_before_current_pointer(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    manifest = make_release(seed, monkeypatch)
    target = tmp_path / "target"
    original = release_seed.atomic_write_json
    def commit(path, payload, **kwargs):
        if Path(path) == target / "snapshots/current.v4.json":
            assert DataSnapshotClient(target / "snapshots").open_generation(manifest.generation_id).is_champion_complete("1")
        return original(path, payload, **kwargs)
    monkeypatch.setattr(release_seed, "atomic_write_json", commit)
    assert release_seed.install_v4_seed(seed, target)["source"] == "verified_v4_seed"
    head = target / "snapshots/current.v4.json"
    previous = head.read_bytes()
    assert release_seed.install_v4_seed(seed, target)["source"] == "runtime_current"
    assert head.read_bytes() == previous


def test_corrupt_seed_never_publishes_current(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    manifest = make_release(seed, monkeypatch)
    (seed / "snapshots/generations" / manifest.generation_id / "manifest.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "target"
    with pytest.raises((ValueError, OSError)):
        release_seed.install_v4_seed(seed, target)
    assert not (target / "snapshots/current.v4.json").exists()
