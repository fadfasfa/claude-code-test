"""发布包 generation 播种测试；最终产品不再提供旧目录迁移。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def test_verified_generation_is_seeded_before_current_pointer(tmp_path, monkeypatch) -> None:
    from hextech.modules.data.generation import DataSnapshotPublisher
    from hextech.infrastructure.persistence.runtime_bundle import seed_bundled_resources

    bundle = tmp_path / "bundle"
    seed_root = bundle / "resources" / "seeds"
    DataSnapshotPublisher(seed_root).publish(
        {
            "champions": [{"id": "1", "name": "英雄一"}],
            "champion_hextech": {"英雄一": {"hero_id": "1", "augments": [{"id": "a1"}]}},
            "overlay_hints": {"augments": {"a1": {"name": "强化一"}}},
            "identities": {"champions": {"1": "英雄一"}, "augments": {"a1": "强化一"}},
        },
        private_stats_enabled=True,
    )
    files = sorted(path for path in seed_root.rglob("*") if path.is_file())
    names = [(Path("resources/seeds") / path.relative_to(seed_root)).as_posix() for path in files]
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in zip(names, files)}
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({"seed_files": names, "seed_sha256": hashes}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "hextech.infrastructure.persistence.runtime_bundle._write_verified_snapshot_startup_status",
        lambda _root: None,
    )
    target = tmp_path / "var" / "snapshots"
    assert seed_bundled_resources(bundle_root=bundle, runtime_snapshot_dir=target) is True
    assert (target / "current.v1.json").is_file()
    assert seed_bundled_resources(bundle_root=bundle, runtime_snapshot_dir=target) is False


def test_corrupt_seed_never_publishes_current(tmp_path) -> None:
    from hextech.infrastructure.persistence.runtime_bundle import seed_bundled_resources

    bundle = tmp_path / "bundle"
    seed = bundle / "resources" / "seeds" / "current.v1.json"
    seed.parent.mkdir(parents=True)
    seed.write_text("{}", encoding="utf-8")
    (bundle / "bundle_manifest.json").write_text(
        json.dumps({"seed_files": ["resources/seeds/current.v1.json"], "seed_sha256": {}}), encoding="utf-8"
    )
    target = tmp_path / "var" / "snapshots"
    assert seed_bundled_resources(bundle_root=bundle, runtime_snapshot_dir=target) is False
    assert not (target / "current.v1.json").exists()
