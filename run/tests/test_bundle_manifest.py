def test_manifest_forbidden_paths_are_matched_by_path_parts():
    from tools.bundle_manifest import manifest_contains_forbidden_path

    manifest = {
        "source_files": [
            "hextech/metadata/runtime-note.json",
            "hextech/cache/data-runtime-summary.py",
            "__pycache__",
            "hextech/__pycache__/module.cpython-311.pyc",
        ],
        "runtime_files": ["data/runtime/cache/example.json"],
    }

    assert manifest_contains_forbidden_path(manifest, "data/runtime")
    assert manifest_contains_forbidden_path(manifest, "__pycache__")
    assert manifest_contains_forbidden_path(manifest, ".pyc")
    assert not manifest_contains_forbidden_path(manifest, "runtime/report")
    assert not manifest_contains_forbidden_path(manifest, "data/raw")
