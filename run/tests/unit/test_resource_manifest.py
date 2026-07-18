import hashlib
import json
from pathlib import Path

import pytest

from tooling.build.resource_manifest import validate_resource_manifest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(root: Path, files: list[dict[str, object]]) -> None:
    manifest_path = root / "resources" / "manifest.v2.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "rules": {
                    "resource_root": "resources",
                    "runtime_root": "var",
                    "runtime_must_not_be_bundled": True,
                    "unlisted_files_must_not_be_bundled": True,
                },
                "files": files,
            }
        ),
        encoding="utf-8",
    )


def _descriptor(root: Path, relative: str, *, role: str = "package") -> dict[str, object]:
    path = root / relative
    return {
        "path": relative,
        "category": "catalog",
        "package_role": role,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def test_validate_resource_manifest_resolves_package_and_unlisted_files(tmp_path: Path) -> None:
    catalog_path = tmp_path / "resources" / "catalog" / "champions.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text("{}", encoding="utf-8")
    extra_path = tmp_path / "resources" / "assets" / "champions" / "extra.png"
    extra_path.parent.mkdir(parents=True)
    extra_path.write_bytes(b"png")
    _write_manifest(tmp_path, [_descriptor(tmp_path, "resources/catalog/champions.json")])

    report = validate_resource_manifest(tmp_path)

    assert report["packaged_files"] == ["resources/catalog/champions.json"]
    assert report["unlisted_files"] == ["resources/assets/champions/extra.png"]


def test_validate_resource_manifest_rejects_path_outside_resources(tmp_path: Path) -> None:
    fixture_path = tmp_path / "tests" / "fixtures" / "sample.json"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text("{}", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [
            {
                "path": "tests/fixtures/sample.json",
                "category": "fixture",
                "package_role": "package",
                "size": fixture_path.stat().st_size,
                "sha256": _sha256(fixture_path),
            }
        ],
    )

    with pytest.raises(ValueError, match="必须位于 resources"):
        validate_resource_manifest(tmp_path)


def test_validate_resource_manifest_rejects_hash_drift(tmp_path: Path) -> None:
    catalog_path = tmp_path / "resources" / "catalog" / "champions.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text("{}", encoding="utf-8")
    descriptor = _descriptor(tmp_path, "resources/catalog/champions.json")
    _write_manifest(tmp_path, [descriptor])
    catalog_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 不一致"):
        validate_resource_manifest(tmp_path)


def test_validate_resource_manifest_requires_unlisted_exclusion_rule(tmp_path: Path) -> None:
    catalog_path = tmp_path / "resources" / "catalog" / "champions.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text("{}", encoding="utf-8")
    _write_manifest(tmp_path, [_descriptor(tmp_path, "resources/catalog/champions.json")])
    manifest_path = tmp_path / "resources" / "manifest.v2.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["rules"]["unlisted_files_must_not_be_bundled"] = False
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="禁止打包未列文件"):
        validate_resource_manifest(tmp_path)
