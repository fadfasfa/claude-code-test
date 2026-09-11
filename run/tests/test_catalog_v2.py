from __future__ import annotations

import json
import hashlib
import io
import shutil
import threading
from pathlib import Path

import pytest
import requests
from PIL import Image

from hextech.infrastructure.sources.catalog_versioned import (
    CatalogRefreshError,
    _build_cdragon_catalog,
    _champion_catalog_payload,
    _champion_catalog_payload_with_filter,
    _freeze_enabled_assets,
    _fetch_icon,
    refresh_catalog,
)
from hextech.modules.data.catalog.version_catalog import load_champion_core_data
from hextech.modules.data.catalog.versioned import (
    CatalogValidationError,
    build_catalog_manifest,
    load_runtime_catalog_from_pointer,
    validate_catalog_files,
)


def _png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), (20, 40, 80)).save(stream, format="PNG")
    return stream.getvalue()


class _IconResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


def _icon_entry() -> dict[str, object]:
    return {
        "cdragon_id": 7001,
        "source_icon_path": "assets/ux/cherry/test.png",
        "source_icon_url": "https://example.invalid/test.png",
    }


def test_cdragon_icon_retries_tls_then_succeeds(tmp_path: Path) -> None:
    calls: list[object] = []
    delays: list[float] = []

    def request_get(_url: str, *, timeout: object) -> _IconResponse:
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol")
        return _IconResponse(200, _png_bytes())

    content = _fetch_icon(
        _icon_entry(),
        tmp_path,
        request_get=request_get,
        sleep_func=delays.append,
    )

    assert content == _png_bytes()
    assert calls == [(5, 20), (5, 20)]
    assert delays == [0.5]


def test_cdragon_icon_retries_timeout_and_429_but_not_404(tmp_path: Path) -> None:
    timeout_delays: list[float] = []

    with pytest.raises(CatalogRefreshError, match="重试耗尽"):
        _fetch_icon(
            _icon_entry(),
            tmp_path,
            request_get=lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.Timeout()),
            sleep_func=timeout_delays.append,
        )
    assert timeout_delays == [0.5, 1.5]

    statuses = iter((429, 200))
    retry_delays: list[float] = []
    assert _fetch_icon(
        _icon_entry(),
        tmp_path,
        request_get=lambda *_args, **_kwargs: _IconResponse(next(statuses), _png_bytes()),
        sleep_func=retry_delays.append,
    ) == _png_bytes()
    assert retry_delays == [0.5]

    calls = 0

    def not_found(*_args, **_kwargs) -> _IconResponse:
        nonlocal calls
        calls += 1
        return _IconResponse(404)

    with pytest.raises(CatalogRefreshError, match="不可重试"):
        _fetch_icon(_icon_entry(), tmp_path, request_get=not_found)
    assert calls == 1


def test_cdragon_icon_reuses_only_hash_verified_canonical_asset(tmp_path: Path) -> None:
    content = _png_bytes()
    digest = hashlib.sha256(content).hexdigest()
    asset = tmp_path / "assets" / "augments" / f"{digest}.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(content)
    (tmp_path / "augment_assets.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "canonical_id": "7001",
                        "relative_path": f"assets/augments/{digest}.png",
                        "sha256": digest,
                        "size": len(content),
                        "source_icon_path": "assets/ux/cherry/test.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    reused = _fetch_icon(
        _icon_entry(),
        tmp_path,
        request_get=lambda *_args, **_kwargs: pytest.fail("verified active asset 不应发起网络请求"),
    )

    assert reused == content


def test_cdragon_icon_retry_honors_worker_cancel(tmp_path: Path) -> None:
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(CatalogRefreshError, match="catalog_refresh_cancelled"):
        _fetch_icon(
            _icon_entry(),
            tmp_path,
            stop_event=stop_event,
            request_get=lambda *_args, **_kwargs: pytest.fail("取消后不应发起网络请求"),
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


def test_remote_champion_candidate_excludes_jade_mode_variants(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "英雄目录.v1.json").write_text('{"aliases": []}', encoding="utf-8")
    canonical = {
        f"Champion{index}": {
            "key": str(index),
            "name": f"英雄{index}",
            "title": f"称号{index}",
            "id": f"Champion{index}",
        }
        for index in range(1, 174)
    }
    jade = {
        f"Jade_Champion{index}": {
            "key": str(60_000 + index),
            "name": f"英雄{index}",
            "title": f"称号{index}",
            "id": f"Jade_Champion{index}",
        }
        for index in range(1, 61)
    }

    payload, source_filter = _champion_catalog_payload_with_filter({**canonical, **jade}, previous)

    assert len(payload["id_to_detail"]) == 173
    assert "173" in payload["id_to_detail"]  # 正常新增英雄不能被第三方统计口径截断。
    assert not any(int(hero_id) >= 60_000 for hero_id in payload["id_to_detail"])
    assert source_filter == {
        "schema_version": 1,
        "upstream_entry_count": 233,
        "canonical_entry_count": 173,
        "excluded_entry_count": 60,
        "excluded_reasons": {"jade_mode_variant": 60},
        "excluded_sample_ids": [str(60_000 + index) for index in range(1, 11)],
    }


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


def test_cdragon_catalog_keeps_pool_outside_minus_one_resources_for_audit(tmp_path: Path) -> None:
    (tmp_path / "海克斯资源目录.v1.json").write_text('{"apexlol_slug_map": {}}', encoding="utf-8")
    payload = _build_cdragon_catalog(
        [
            {
                "id": -1,
                "augmentNameId": "Special_Death_and_Taxes",
                "nameTRA": "死与税",
                "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Strawberry/death.png",
                "rarity": "kSilver",
            },
            {
                "id": -1,
                "augmentNameId": "Weapon_RapidShot_Evolve",
                "nameTRA": "OwO魔爆炮",
                "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Strawberry/weapon.png",
                "rarity": "kGold",
            },
        ],
        tmp_path,
    )

    assert len(payload["entries"]) == 2
    assert {item["augment_name_id"] for item in payload["entries"]} == {
        "Special_Death_and_Taxes",
        "Weapon_RapidShot_Evolve",
    }


def test_enabled_pool_requires_one_catalog_identity_per_id(tmp_path: Path) -> None:
    payload = {
        "entries": [
            {"cdragon_id": 7001, "name": "升级：兹若特传送门"},
            {"cdragon_id": 7001, "name": "重复身份"},
        ]
    }

    with pytest.raises(CatalogRefreshError, match="duplicates"):
        _freeze_enabled_assets(tmp_path, payload, {"7001"}, tmp_path)


def test_runtime_catalog_pointer_rejects_path_traversal() -> None:
    pointer = {
        "schema_version": 2,
        "catalog_generation_id": "../outside",
        "manifest_sha256": "a" * 64,
    }

    assert load_runtime_catalog_from_pointer(pointer) is None
