"""Seed integrity moved to test_release_v4; this module only reexports synthetic fixtures."""
from support.v4_release import _write_catalog as _write_catalog

def _fixture_runtime(tmp_path):
    from support.v4_release import release_runtime
    root, generation, _ = release_runtime(tmp_path)
    return root, {"generation_id": generation}
