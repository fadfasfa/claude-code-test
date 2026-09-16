"""Mayhem 刷新健康与 last-good 保留测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def test_recent_success_is_not_due(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: {"last_success_at": _iso(1000)})
    assert service.mayhem_refresh_due(now=1100, stale_after_seconds=200) is False
    assert service.mayhem_refresh_due(now=1201, stale_after_seconds=200) is True


def test_default_mayhem_check_interval_is_four_hours() -> None:
    from hextech.infrastructure.sources.mayhem import service

    assert service.MAYHEM_STALE_SECONDS == 4 * 60 * 60


def test_failure_retry_uses_stable_jitter(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    status = {"last_attempt_at": _iso(1000), "last_result": "failed", "reason": "network_error"}
    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: status)
    jitter = service._stable_failure_retry_jitter_seconds(status, 300)
    assert service.mayhem_refresh_due(
        now=1000 + 1800 + jitter - 1,
        failure_retry_seconds=1800,
        failure_retry_jitter_seconds=300,
    ) is False
    assert service.mayhem_refresh_due(
        now=1000 + 1800 + jitter,
        failure_retry_seconds=1800,
        failure_retry_jitter_seconds=300,
    ) is True


def test_skipped_refresh_preserves_last_success(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    previous = {"last_success_at": _iso(1000), "last_result": "success"}
    written: dict = {}
    monkeypatch.setattr(service, "load_mayhem_refresh_status", lambda: previous)
    monkeypatch.setattr(service, "get_mayhem_refresh_status_path", lambda: "unused.json")
    monkeypatch.setattr(service, "atomic_write_json", lambda _path, payload, **_kwargs: written.update(payload))
    payload = service.write_mayhem_refresh_status(result="skipped", reason="not_stale", now=1100)
    assert payload["last_success_at"] == previous["last_success_at"]
    assert written["last_result"] == "skipped"


def test_empty_online_result_does_not_publish(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    monkeypatch.setattr(service, "write_mayhem_refresh_status", lambda **kwargs: kwargs)
    payload = service.run_mayhem_refresh(force=True, scraper=lambda: {"items": [], "rejects": []})
    assert payload["result"] == "failed"
    assert payload["reason"] == "raw_empty"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_mayhem_validation_does_not_require_apex_current(tmp_path) -> None:
    from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

    raw_path = tmp_path / "mayhem.json"
    augment_path = tmp_path / "augments.json"
    champion_path = tmp_path / "champions.json"
    _write_json(
        raw_path,
        {
            "items": [
                {
                    "id": "combo-1",
                    "champion_id": "24",
                    "augment_names": ["测试海克斯"],
                    "body": "测试联动",
                }
            ],
            "rejects": [],
        },
    )
    _write_json(augment_path, [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(champion_path, {"24": {"name": "武器大师", "en_name": "Jax"}})

    summary = merge_mayhem_combos(
        mayhem_raw_path=raw_path,
        augment_manifest_path=augment_path,
        core_data_path=champion_path,
        validate_only=True,
    )

    assert summary["base_mode"] == "validation_only"
    assert summary["apex_path"] == ""
    assert summary["mayhem_valid_items"] == 1
    assert summary["written"] is False


def test_mayhem_merge_requires_explicit_apex_and_never_overrides_it(tmp_path) -> None:
    from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

    raw_path = tmp_path / "mayhem.json"
    apex_path = tmp_path / "apex.json"
    augment_path = tmp_path / "augments.json"
    champion_path = tmp_path / "champions.json"
    combo = {
        "champion_id": "24",
        "augment_names": ["测试海克斯"],
        "body": "Mayhem 不得覆盖 Apex",
    }
    _write_json(raw_path, {"items": [combo], "rejects": []})
    _write_json(
        apex_path,
        {
            "24": {
                "id": "24",
                "name": "武器大师",
                "synergy_items": [{"augment_names": ["测试海克斯"], "source": "apex"}],
                "synergies": [],
            }
        },
    )
    _write_json(augment_path, [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(champion_path, {"24": {"name": "武器大师", "en_name": "Jax"}})

    summary = merge_mayhem_combos(
        apex_path=apex_path,
        mayhem_raw_path=raw_path,
        augment_manifest_path=augment_path,
        core_data_path=champion_path,
    )

    assert summary["base_mode"] == "apex_input"
    assert summary["mayhem_valid_items"] == 1
    assert summary["added_items"] == 0
    assert summary["skipped_duplicate_items"] == 1
    assert summary["merged_payload"]["24"]["synergy_items"][0]["source"] == "apex"


def _mayhem_raw(*, revision="u" * 64, items=None, not_modified=False):
    return {
        "schema_version": 1,
        "source": "arammayhem",
        "source_url": "https://arammayhem.com/zh-cn/combo/",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "items": items if items is not None else [{"champion_id": "24", "body": "测试"}],
        "rejects": [],
        "upstream_revision": revision,
        "transport": {"not_modified": not_modified, "request_count": 2},
    }


def test_mayhem_304_reuses_cached_manifest_without_second_download(tmp_path, monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import source
    from hextech.infrastructure.transport.conditional_response import ConditionalResponseCache
    from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult

    page_url = source.DEFAULT_COMBO_URL
    manifest_url = "https://arammayhem.com/static/combos.json"
    html = f'<html data-clock="one"><div data-combo-manifest-url="{manifest_url}"></div></html>'
    manifest = json.dumps({
        "pageSize": 1,
        "totalCombos": 1,
        "cards": [{
            "championId": "24",
            "augmentName": "测试海克斯",
            "comboDescription": "测试联动",
        }],
    }, ensure_ascii=False)
    calls = []

    def request(url, **kwargs):
        calls.append((url, dict(kwargs.get("headers") or {})))
        cached = len(calls) > 2
        body = html if url == page_url else manifest
        etag = '"page-v1"' if url == page_url else '"manifest-v1"'
        return ScraplingFetchResult(
            url,
            "" if cached else body,
            304 if cached else 200,
            "fixture",
            "",
            response_headers={"ETag": etag},
        )

    monkeypatch.setattr(source, "fetch_text", request)
    cache = ConditionalResponseCache(tmp_path, source="mayhem")
    first = source.scrape_mayhem_combos(conditional_cache=cache)
    second = source.scrape_mayhem_combos(conditional_cache=cache)

    assert first["upstream_revision"] == second["upstream_revision"]
    assert second["transport"]["not_modified"] is True
    assert len(calls) == 4
    assert calls[2][1]["If-None-Match"] == '"page-v1"'
    assert calls[3][1]["If-None-Match"] == '"manifest-v1"'


def test_mayhem_page_clock_change_does_not_change_business_revision(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import source
    from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult

    manifest_url = "https://arammayhem.com/static/combos.json"
    manifest = json.dumps({
        "cards": [{
            "championId": "24",
            "augmentName": "测试海克斯",
            "comboDescription": "测试联动",
        }]
    }, ensure_ascii=False)
    clock = iter(("one", "one", "two", "two"))

    def request(url, **_kwargs):
        marker = next(clock)
        text = (
            f'<div data-clock="{marker}" data-combo-manifest-url="{manifest_url}"></div>'
            if url == source.DEFAULT_COMBO_URL
            else manifest
        )
        return ScraplingFetchResult(url, text, 200, "fixture", "")

    monkeypatch.setattr(source, "fetch_text", request)
    first = source.scrape_mayhem_combos()
    second = source.scrape_mayhem_combos()
    assert first["upstream_revision"] == second["upstream_revision"]


def test_mayhem_same_revision_skips_merge_and_publish(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    catalog = SimpleNamespace(generation_id="catalog", content_sha256="a" * 64)
    monkeypatch.setattr(service, "load_active_catalog", lambda: catalog)
    monkeypatch.setattr(
        service,
        "_current_manifest",
        lambda: (
            {
                "run_id": "mayhem-current",
                "catalog_generation_id": "catalog",
                "catalog_sha256": "a" * 64,
            },
            {
                "upstream_revision": "u" * 64,
                "applied_revision": "p" * 64,
                "parser_revision": service.MAYHEM_PROJECTION_REVISION,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "write_mayhem_refresh_status",
        lambda **kwargs: {**kwargs, **dict(kwargs.get("extra") or {})},
    )
    for _ in range(42):
        result = service.run_mayhem_refresh(
            force=True,
            scraper=lambda: _mayhem_raw(not_modified=False),
            merge=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("merge must be skipped")),
        )
        assert result["success"] is True
        assert result["reason"] == "not_stale"
        assert result["check_status"] == "up_to_date"
        assert result["upstream_revision"] == result["applied_revision"] == "p" * 64
        assert result["representation_revision"] == "u" * 64


def test_mayhem_changed_revision_publishes_bound_metadata(tmp_path, monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    catalog = SimpleNamespace(
        generation_id="catalog",
        content_sha256="a" * 64,
        root=tmp_path,
    )
    monkeypatch.setattr(service, "load_active_catalog", lambda: catalog)
    monkeypatch.setattr(service, "_current_manifest", lambda: ({}, {}))
    monkeypatch.setattr(
        service,
        "load_source_current",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("invalid current was already rejected")),
    )
    monkeypatch.setattr(
        service,
        "write_mayhem_refresh_status",
        lambda **kwargs: {**kwargs, **dict(kwargs.get("extra") or {})},
    )
    captured = {}

    def publish(*_args, **kwargs):
        captured.update(kwargs)
        return "artifact.json", None

    monkeypatch.setattr(service, "publish_mayhem_run", publish)
    result = service.run_mayhem_refresh(
        force=True,
        scraper=lambda: _mayhem_raw(revision="n" * 64),
        merge=lambda **_kwargs: {
            "added_items": 1,
            "mayhem_raw_items": 1,
            "mayhem_valid_items": 1,
            "normalized_items": [{"champion_id": "24", "body": "测试"}],
            "clean_rejects": [],
        },
    )
    assert result["success"] is True and result["check_status"] == "changed"
    assert result["upstream_revision"] == result["applied_revision"]
    assert result["representation_revision"] == "n" * 64
    assert captured["upstream_revision"] == "n" * 64
    assert captured["applied_revision"] == result["applied_revision"]
    assert captured["parser_revision"] == service.MAYHEM_PROJECTION_REVISION


def test_mayhem_repeated_failed_revision_skips_merge_for_six_hours(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    catalog = SimpleNamespace(generation_id="catalog", content_sha256="a" * 64)
    fingerprint = service._failure_fingerprint(
        "u" * 64,
        service.MAYHEM_PROJECTION_REVISION,
        catalog.generation_id,
        catalog.content_sha256,
        validation_input=_mayhem_raw(),
    )
    monkeypatch.setattr(service, "load_active_catalog", lambda: catalog)
    monkeypatch.setattr(service, "_current_manifest", lambda: ({}, {}))
    monkeypatch.setattr(
        service,
        "load_mayhem_refresh_status",
        lambda: {"failure_fingerprint": fingerprint},
    )
    monkeypatch.setattr(
        service,
        "write_mayhem_refresh_status",
        lambda **kwargs: {**kwargs, **dict(kwargs.get("extra") or {})},
    )
    result = service.run_mayhem_refresh(
        force=True,
        scraper=lambda: _mayhem_raw(),
        merge=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("merge must be skipped")),
    )
    assert result["success"] is False
    assert result["reason"] == "repeated_invalid_content"
    assert result["retry_after_seconds"] == service.MAYHEM_REPEAT_INVALID_RETRY_SECONDS


def test_mayhem_semantic_same_does_not_create_orphan_source_run(tmp_path, monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    items = [{"champion_id": "24", "body": "测试"}]
    normalized_payload = {
        "schema_version": 2,
        "source": "arammayhem",
        "source_url": "https://arammayhem.com/zh-cn/combo/",
        "fetched_at": "ignored",
        "items": items,
    }
    applied = service._semantic_revision(normalized_payload)
    pointer = {
        "run_id": "mayhem-current",
        "catalog_generation_id": "catalog",
        "catalog_sha256": "a" * 64,
        "artifact": {
            "role": "combos",
            "relative_path": "combos.json",
            "sha256": "b" * 64,
            "record_count": 1,
            "content_schema_version": 1,
            "size": 1,
        },
    }
    monkeypatch.setattr(
        service,
        "load_active_catalog",
        lambda: SimpleNamespace(
            generation_id="catalog", content_sha256="a" * 64, root=tmp_path
        ),
    )
    monkeypatch.setattr(
        service,
        "_current_manifest",
        lambda: (
            pointer,
            {
                "upstream_revision": "old-representation",
                "applied_revision": applied,
                "parser_revision": service.MAYHEM_PROJECTION_REVISION,
            },
        ),
    )
    monkeypatch.setattr(service, "write_mayhem_refresh_status", lambda **kwargs: {**kwargs, **dict(kwargs.get("extra") or {})})
    monkeypatch.setattr(
        service,
        "source_run_artifact_path",
        lambda *_a, **_k: pytest.fail("semantic check must not allocate a run artifact"),
    )
    monkeypatch.setattr(
        service,
        "publish_mayhem_run",
        lambda *_a, **_k: pytest.fail("semantic same must not publish"),
    )

    result = service.run_mayhem_refresh(
        force=True,
        scraper=lambda: _mayhem_raw(revision="new-representation", items=items),
        merge=lambda **_kwargs: {
            "added_items": 1,
            "mayhem_raw_items": 1,
            "mayhem_valid_items": 1,
            "normalized_items": items,
            "clean_rejects": [],
        },
    )

    assert result["success"] is True
    assert result["check_status"] == "up_to_date"
    assert result["upstream_revision"] == result["applied_revision"] == applied


def test_default_mayhem_projection_uses_in_memory_raw_payload(tmp_path) -> None:
    from hextech.infrastructure.sources.mayhem import service

    _write_json(tmp_path / "海克斯资源目录.v1.json", [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(tmp_path / "英雄目录.v1.json", {"24": {"name": "武器大师", "en_name": "Jax"}})
    summary = service._merge_candidate(
        {
            "items": [{
                "champion_id": "24",
                "augment_names": ["测试海克斯"],
                "body": "测试联动",
            }],
            "rejects": [],
        },
        catalog_root=tmp_path,
        merge=None,
    )

    assert summary["mayhem_valid_items"] == 1
    assert not (tmp_path / "sources").exists()


def test_mayhem_source_result_honors_server_retry_after(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service

    raw = _mayhem_raw(items=[])
    raw["transport"] = {
        "check_complete": False,
        "retry_after_seconds": 43200,
    }
    monkeypatch.setattr(
        service,
        "load_active_catalog",
        lambda: SimpleNamespace(generation_id="catalog", content_sha256="a" * 64),
    )
    monkeypatch.setattr(service, "_current_manifest", lambda: ({}, {}))
    monkeypatch.setattr(service, "write_mayhem_refresh_status", lambda **kwargs: {**kwargs, **dict(kwargs.get("extra") or {})})

    result = service.run_mayhem_refresh(force=True, scraper=lambda: raw)

    assert result["success"] is False
    assert result["retry_after_seconds"] == 43200


def test_mayhem_transport_extracts_retry_after(monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import source
    from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult

    monkeypatch.setattr(
        source,
        "fetch_text",
        lambda url, **_kwargs: ScraplingFetchResult(
            url,
            "",
            429,
            "fixture",
            "rate_limited",
            response_headers={"Retry-After": "43200"},
        ),
    )

    result = source.scrape_mayhem_combos()

    assert result["transport"]["retry_after_seconds"] == 43200


def test_mayhem_repaired_rejects_retry_same_business_revision(tmp_path, monkeypatch) -> None:
    from hextech.infrastructure.sources.mayhem import service, source
    from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult

    _write_json(tmp_path / "海克斯资源目录.v1.json", [{"name": "测试海克斯", "tier": "黄金"}])
    _write_json(tmp_path / "英雄目录.v1.json", {"24": {"name": "武器大师", "en_name": "Jax"}})
    monkeypatch.setattr(service, "load_active_catalog", lambda: SimpleNamespace(
        generation_id="catalog", content_sha256="a" * 64, root=tmp_path,
    ))
    monkeypatch.setattr(service, "_current_manifest", lambda: ({}, {}))
    monkeypatch.setattr(service, "get_mayhem_refresh_status_path", lambda: str(tmp_path / "status.json"))
    cards = [{
        "championId": "24", "augmentName": "测试海克斯", "comboDescription": "测试联动",
    }, {"id": "invalid"}]
    manifest_url = "https://arammayhem.com/static/combos.json"

    def fetch(url, **_kwargs):
        text = (
            f'<div data-combo-manifest-url="{manifest_url}"></div>'
            if url == source.DEFAULT_COMBO_URL
            else json.dumps({"cards": cards, "totalCombos": len(cards)}, ensure_ascii=False)
        )
        return ScraplingFetchResult(url, text, 200, "fixture", "", response_headers={"Retry-After": "43200"})

    monkeypatch.setattr(source, "fetch_text", fetch)
    published = []
    monkeypatch.setattr(service, "publish_mayhem_run", lambda payload, **kwargs: (
        published.append((payload, kwargs)) or "artifact.json", None,
    ))
    merge = service._merge_candidate
    merges = []

    def project(*args, **kwargs):
        merges.append(True)
        return merge(*args, **kwargs)

    monkeypatch.setattr(service, "_merge_candidate", project)
    broken_revision = source.scrape_mayhem_combos()["upstream_revision"]
    first = service.run_mayhem_refresh(force=True)
    assert first["reason"] == "reject_ratio_exceeded"
    repeated = service.run_mayhem_refresh(force=True)
    assert repeated["reason"] == "repeated_invalid_content"
    assert repeated["failure_fingerprint"] == first["failure_fingerprint"]
    assert repeated["retry_after_seconds"] == 43200
    assert not merges and not published

    cards.pop()
    assert source.scrape_mayhem_combos()["upstream_revision"] == broken_revision
    repaired = service.run_mayhem_refresh(
        force=True, previous_failure_fingerprint=first["failure_fingerprint"],
    )
    assert repaired["success"] is True
    assert repaired["check_status"] == "changed"
    assert len(merges) == len(published) == 1
    assert published[0][1]["report"]["pagination_complete"] is True
    assert not (tmp_path / "sources").exists()


@pytest.mark.parametrize("changed", [
    {"rejects": [{"reason": "missing_required_fields"}]},
    {"page": {"pagination_complete": False, "selected": 1, "total": 2}},
    {"max_pages": 1},
    {"transport": {"check_complete": False}},
])
def test_mayhem_failure_identity_binds_validation_input(changed) -> None:
    from hextech.infrastructure.sources.mayhem import service

    raw = _mayhem_raw()
    raw.update(page={"pagination_complete": True, "selected": 1, "total": 1}, max_pages=0)
    raw["transport"]["check_complete"] = True

    def fingerprint(payload):
        return service._failure_fingerprint(
            payload["upstream_revision"], service.MAYHEM_PROJECTION_REVISION,
            "catalog", "a" * 64, validation_input=payload,
        )

    assert fingerprint(raw) != fingerprint({**raw, **changed})
    assert fingerprint(raw) == fingerprint({
        **raw, "fetched_at": "2026-09-16T00:00:00Z",
        "transport": {"check_complete": True, "not_modified": True, "retry_after_seconds": 43200},
    })
