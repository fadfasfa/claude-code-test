from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hextech.infrastructure.sources.catalog_versioned import CatalogRefreshError, _champion_catalog_payload, refresh_catalog
from hextech.modules.data.catalog.version_catalog import load_champion_core_data
from hextech.modules.data.catalog.versioned import (
    CatalogValidationError,
    build_catalog_manifest,
    validate_catalog_files,
)


def test_remote_champion_candidate_rebuilds_all_catalog_indexes(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "英雄目录.v1.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {"heroId": "1", "aliases": ["ann"]},
                    {"heroId": "2", "aliases": ["ola"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = _champion_catalog_payload(
        {
            "Annie": {"key": "1", "name": "黑暗之女", "title": "安妮", "id": "Annie"},
            "Olaf": {"key": "2", "name": "狂战士", "title": "奥拉夫", "id": "Olaf"},
        },
        previous,
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "英雄目录.v1.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    core = load_champion_core_data(candidate)

    assert set(payload) == {
        "schema_version",
        "description",
        "aliases",
        "alias_to_id",
        "id_to_name",
        "id_to_detail",
    }
    assert set(core) == {"1", "2"}
    assert core["1"]["en_name"] == "Annie"
    assert payload["alias_to_id"]["ann"] == "1"
    assert payload["alias_to_id"]["annie"] == "1"


def test_catalog_validation_rejects_aliases_without_business_indexes(tmp_path: Path) -> None:
    resources = Path(__file__).resolve().parents[1] / "resources" / "catalog"
    shutil.copy2(resources / "海克斯资源目录.v1.json", tmp_path / "海克斯资源目录.v1.json")
    shutil.copy2(resources / "hero_version.txt", tmp_path / "hero_version.txt")
    (tmp_path / "英雄目录.v1.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {
                        "heroId": "1",
                        "heroName": "黑暗之女",
                        "title": "安妮",
                        "enName": "Annie",
                        "aliases": ["ann"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = build_catalog_manifest(tmp_path, created_at="fixture")

    with pytest.raises(CatalogValidationError, match="英雄投影不完整"):
        validate_catalog_files(tmp_path, manifest)


def test_catalog_refresh_rejects_direct_current_promotion() -> None:
    with pytest.raises(CatalogRefreshError, match="cohort promotion"):
        refresh_catalog(promote_current=True, allow_remote=False)
