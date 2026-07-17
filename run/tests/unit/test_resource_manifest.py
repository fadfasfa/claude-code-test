import json
from pathlib import Path

import pytest

from tooling.build.resource_manifest import validate_resource_manifest


def _write_manifest(root: Path, categories: list[dict[str, object]]) -> None:
    manifest_path = root / "resources" / "manifest.v1.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": {
                    "resource_root": "resources",
                    "runtime_root": "var",
                    "runtime_must_not_be_bundled": True,
                },
                "categories": categories,
            }
        ),
        encoding="utf-8",
    )


def test_validate_resource_manifest_resolves_stable_and_forbidden_categories(tmp_path: Path) -> None:
    catalog_path = tmp_path / "resources" / "catalog" / "champions.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text("{}", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [
            {
                "name": "catalog",
                "path": "resources/catalog",
                "source_globs": ["resources/catalog/*.json"],
                "package_role": "required",
            },
            {
                "name": "runtime",
                "path": "var",
                "source_globs": ["var/**/*"],
                "package_role": "forbidden",
            },
        ],
    )

    resolved = validate_resource_manifest(tmp_path)

    assert resolved["catalog"] == ["resources/catalog/champions.json"]
    assert resolved["runtime"] == []


def test_validate_resource_manifest_rejects_stable_resource_glob_outside_resources(tmp_path: Path) -> None:
    fixture_path = tmp_path / "tests" / "fixtures" / "sample.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text("{}", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [
            {
                "name": "catalog",
                "path": "resources/catalog",
                "source_globs": ["tests/fixtures/*.json"],
                "package_role": "required",
            }
        ],
    )

    with pytest.raises(ValueError, match="稳定资源 glob 越界"):
        validate_resource_manifest(tmp_path)


def test_validate_resource_manifest_requires_forbidden_role_for_runtime(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        [
            {
                "name": "runtime",
                "path": "var",
                "source_globs": ["var/**/*"],
                "package_role": "optional",
            }
        ],
    )

    with pytest.raises(ValueError, match="必须标记为 forbidden"):
        validate_resource_manifest(tmp_path)
