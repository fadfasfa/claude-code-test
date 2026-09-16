from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hextech.infrastructure.sources.blitz import service
from hextech.infrastructure.sources.blitz.schema import BlitzSchemaError, normalize_payload, validate_artifact
from hextech.modules.data import source_runs


def _row(augment_id: int, *, patch: str = "16.16", data_date: str = "2026-08-14") -> dict[str, Any]:
    return {
        "augment_id": str(augment_id),
        "dt": data_date,
        "dummy": "dummy",
        "patch": patch,
        "stats": {
            "tier": (augment_id % 5) + 1,
            "top_champions": [
                {"champion_id": "1", "tier": 2},
                {"champion_id": "2", "tier": 4},
            ],
        },
    }


class FixtureFetcher:
    def __init__(self, payload: object, *, status_code: int = 200, error: str = "") -> None:
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url: str, **_: object) -> object:
        self.calls.append(url)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {
            "text": text,
            "status_code": self.status_code,
            "error": self.error,
            "error_kind": "http_403" if self.status_code == 403 else "",
            "elapsed_ms": 12,
            "attempts": 1,
            "fetched_at": "2026-08-15T00:00:00+00:00",
        }


class ConditionalFixtureFetcher(FixtureFetcher):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__({})
        self.responses = list(responses)
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, url: str, **kwargs: object) -> object:
        self.calls.append(url)
        self.kwargs.append(dict(kwargs))
        return self.responses.pop(0)


@pytest.fixture
def isolated_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    return tmp_path


def _binding(
    *,
    production_ids: set[str] | None = None,
    compatible_extra_augment_ids: set[str] | None = None,
) -> service.CatalogBinding:
    ids = frozenset(production_ids or {"1001", "1002", "7001", "7002"})
    return service.CatalogBinding(
        generation_id="catalog-fixture",
        content_sha256="c" * 64,
        augment_ids=ids | frozenset({"2000"}),
        champion_ids=frozenset({"1", "2"}),
        production_ids=ids,
        compatible_extra_augment_ids=frozenset(compatible_extra_augment_ids or set()),
    )


def test_schema_normalizes_marker_and_rejects_mixed_patch_or_duplicate() -> None:
    normalized = normalize_payload({"data": [_row(7002), _row(1001)]})

    assert [item["augment_id"] for item in normalized["rows"]] == ["1001", "7002"]
    assert normalized["marker"]["record_count"] == 2
    assert len(normalized["marker"]["content_sha256"]) == 64
    assert validate_artifact(normalized) == normalized

    with pytest.raises(BlitzSchemaError, match="多个 patch"):
        normalize_payload({"data": [_row(1001), _row(1002, patch="16.15")]})
    with pytest.raises(BlitzSchemaError, match="重复海克斯"):
        normalize_payload({"data": [_row(1001), _row(1001)]})


def test_refresh_publishes_hash_bound_ranking_candidate(isolated_sources: Path) -> None:
    fetcher = FixtureFetcher({"data": [_row(value) for value in (1001, 1002, 7001, 7002)]})
    pointer_output = isolated_sources / "candidates" / "blitz.v2.json"

    result = service.refresh_blitz(
        force=True,
        pointer_output=pointer_output,
        fetcher=fetcher,
        catalog_binding=_binding(),
    )

    assert result["success"] is True
    assert result["coverage"]["covered_count"] == 4
    assert fetcher.calls == [service.DATA_URL]
    pointer = json.loads(pointer_output.read_text(encoding="utf-8"))
    artifact = service.validate_blitz_artifact(pointer)
    assert artifact["patch"] == "16.16"
    assert len(artifact["rows"]) == 4
    assert not (isolated_sources / "sources" / "blitz" / "current.v2.json").exists()


def test_refresh_filters_only_verified_adoption_ids_and_recomputes_artifact_marker(
    isolated_sources: Path,
) -> None:
    source_ids = (1001, 1002, 7001, 7002, 2141, 2157)
    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": [_row(value) for value in source_ids]}),
        catalog_binding=_binding(compatible_extra_augment_ids={"2141", "2157"}),
    )

    assert result["success"] is True
    assert result["marker"]["record_count"] == 6
    assert result["compatibility_filtered_augment_ids"] == ["2141", "2157"]
    pointer = dict(result["pointer"])
    artifact_path = source_runs.source_run_artifact_path(
        "blitz",
        pointer["run_id"],
        pointer["artifact"]["relative_path"],
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert [row["augment_id"] for row in artifact["rows"]] == ["1001", "1002", "7001", "7002"]
    assert artifact["marker"]["record_count"] == 4
    assert artifact["marker"]["content_sha256"] != result["marker"]["content_sha256"]
    assert artifact["coverage"]["compatibility_filtered_augment_ids"] == ["2141", "2157"]
    assert validate_artifact(artifact)["marker"] == artifact["marker"]

    run_dir = source_runs.source_run_dir("blitz", pointer["run_id"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["compatibility_filtered_augment_ids"] == ["2141", "2157"]
    assert report["compatibility_filtered_augment_ids"] == ["2141", "2157"]


def test_refresh_rejects_low_production_coverage_and_unknown_identity(isolated_sources: Path) -> None:
    low = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": [_row(1001)]}),
        catalog_binding=_binding(),
    )
    assert low["success"] is False
    assert low["reason"] == "production_coverage_insufficient"
    assert low["diagnostics"]["missing_production_count"] == 3
    assert low["diagnostics"]["missing_production_ids"] == ["1002", "7001", "7002"]

    unknown = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": [_row(value) for value in (1001, 1002, 7001, 9999)]}),
        catalog_binding=_binding(),
    )
    assert unknown["success"] is False
    assert unknown["reason"] == "catalog_binding_failed"

    unknown_champion_row = _row(1001)
    unknown_champion_row["stats"]["top_champions"][0]["champion_id"] = "999"
    unknown_champion = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(
            {
                "data": [
                    unknown_champion_row,
                    _row(1002),
                    _row(7001),
                    _row(7002),
                ]
            }
        ),
        catalog_binding=_binding(),
    )
    assert unknown_champion["success"] is False
    assert unknown_champion["reason"] == "catalog_binding_failed"
    assert unknown_champion["diagnostics"]["unknown_champion_ids"] == ["999"]


def _partial_coverage_fixture() -> tuple[service.CatalogBinding, list[int], list[int]]:
    production = list(range(10_001, 10_238))
    catalog_only = list(range(20_001, 20_205))
    binding = service.CatalogBinding(
        generation_id="catalog-partial-fixture",
        content_sha256="d" * 64,
        augment_ids=frozenset(str(value) for value in (*production, *catalog_only)),
        champion_ids=frozenset({"1", "2"}),
        production_ids=frozenset(str(value) for value in production),
    )
    return binding, production, catalog_only


def _install_verified_partial_fixture_last_good(
    isolated_sources: Path,
) -> tuple[service.CatalogBinding, list[int], list[int], dict[str, Any]]:
    binding, production, catalog_only = _partial_coverage_fixture()
    last_good_rows = [
        _row(value, patch="16.16", data_date="2026-08-25")
        for value in (*production, *catalog_only)
    ]
    last_good = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": last_good_rows}),
        catalog_binding=binding,
    )
    assert last_good["success"] is True
    assert last_good["record_count"] == 441
    current = source_runs.source_current_path("blitz")
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(json.dumps(last_good["pointer"]), encoding="utf-8")
    return binding, production, catalog_only, dict(last_good["pointer"])


def test_active_partial_accepts_exact_16_17_fixture_without_mixing_last_good_rows(
    isolated_sources: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hextech.bootstrap.blitz_generation import build_blitz_details
    from hextech.modules.data.catalog import version_catalog

    binding, production, catalog_only, previous = _install_verified_partial_fixture_last_good(
        isolated_sources
    )
    current_production = production[:207]
    latest_rows = [
        _row(value, patch="16.17", data_date="2026-08-31")
        for value in (*current_production, *catalog_only)
    ]

    strict = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": latest_rows}),
        catalog_binding=binding,
        coverage_policy="catalog_adoption",
    )
    assert strict["success"] is False
    assert strict["reason"] == "production_coverage_insufficient"

    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher({"data": latest_rows}),
        catalog_binding=binding,
        coverage_policy="active_partial",
    )

    assert result["success"] is True
    assert result["reason"] == "partial_ready"
    coverage = result["coverage"]
    assert coverage["coverage_state"] == "partial_ready"
    assert coverage["coverage_policy"] == "active_partial"
    assert coverage["production_count"] == 237
    assert coverage["covered_count"] == 207
    assert coverage["coverage_ratio"] == pytest.approx(207 / 237)
    assert coverage["missing_ids"] == [str(value) for value in production[207:]]
    assert coverage["source_record_count"] == 411
    assert coverage["filtered_source_record_count"] == 411
    assert coverage["previous_record_count"] == 441
    assert coverage["previous_record_ratio"] == pytest.approx(411 / 441)
    assert coverage["compatibility_filtered_augment_ids"] == []
    assert source_runs.load_source_current("blitz", verify_hash=True)["run_id"] == previous["run_id"]
    artifact = service.validate_blitz_artifact(result["pointer"])
    assert artifact["patch"] == "16.17"
    assert len(artifact["rows"]) == 411
    assert {row["augment_id"] for row in artifact["rows"]}.isdisjoint(
        {str(value) for value in production[207:]}
    )

    catalog_root = isolated_sources / "catalog-partial-fixture"
    catalog_root.mkdir(parents=True)
    (catalog_root / "augment_assets.v1.json").write_text(
        json.dumps({"entries": [{"canonical_id": str(value)} for value in production]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        version_catalog,
        "load_augment_manifest_entries",
        lambda _root: [
            {
                "cdragon_id": str(value),
                "name": f"海克斯 {value}",
                "tier": "Silver",
                "icon_url": "",
                "augment_name_id": f"augment-{value}",
            }
            for value in production
        ],
    )
    monkeypatch.setattr(
        version_catalog,
        "load_champion_core_data",
        lambda _root: {"1": {"name": "测试英雄"}},
    )
    details = build_blitz_details(
        result["pointer"],
        catalog=SimpleNamespace(root=catalog_root),
    )
    cards = details["测试英雄"]["augments"]
    assert len(cards) == 207
    assert {card["id"] for card in cards} == {str(value) for value in current_production}
    assert {card["source_patch"] for card in cards} == {"16.17"}


def test_active_partial_fails_closed_below_coverage_or_previous_record_floor(
    isolated_sources: Path,
) -> None:
    binding, production, catalog_only, _previous = _install_verified_partial_fixture_last_good(
        isolated_sources
    )

    below_coverage = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(
            {
                "data": [
                    _row(value, patch="16.17", data_date="2026-08-31")
                    for value in (*production[:201], *catalog_only)
                ]
            }
        ),
        catalog_binding=binding,
        coverage_policy="active_partial",
    )
    assert below_coverage["success"] is False
    assert below_coverage["reason"] == "production_coverage_insufficient"

    below_previous_record_ratio = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(
            {
                "data": [
                    _row(value, patch="16.17", data_date="2026-08-31")
                    for value in (*production[:207], *catalog_only[:188])
                ]
            }
        ),
        catalog_binding=binding,
        coverage_policy="active_partial",
    )
    assert below_previous_record_ratio["success"] is False
    assert below_previous_record_ratio["reason"] == "source_record_ratio_insufficient"
    assert below_previous_record_ratio["diagnostics"]["previous_record_ratio"] == pytest.approx(
        395 / 441
    )


def test_active_partial_without_verified_last_good_still_requires_95_percent(
    isolated_sources: Path,
) -> None:
    binding, production, catalog_only = _partial_coverage_fixture()
    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(
            {
                "data": [
                    _row(value, patch="16.17", data_date="2026-08-31")
                    for value in (*production[:207], *catalog_only)
                ]
            }
        ),
        catalog_binding=binding,
        coverage_policy="active_partial",
    )

    assert result["success"] is False
    assert result["reason"] == "production_coverage_insufficient"
    assert result["diagnostics"]["required_coverage_ratio"] == 0.95


def test_refresh_fails_closed_on_403(isolated_sources: Path) -> None:
    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher("forbidden", status_code=403),
        catalog_binding=_binding(),
    )

    assert result["success"] is False
    assert result["reason"] == "blocked"
    manifest = json.loads(
        (source_runs.source_run_dir("blitz", result["run_id"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["outcomes"][0]["failure_kind"] == "http_403"


def test_429_retry_after_is_preserved_in_source_result(isolated_sources: Path) -> None:
    response = {
        "status_code": 429,
        "text": "",
        "error": "http_429",
        "error_kind": "http_429",
        "response_headers": {"Retry-After": "180"},
    }

    result = service.refresh_blitz(
        fetcher=lambda *_args, **_kwargs: response,
        catalog_binding=_binding(),
    )

    assert result["success"] is False
    assert result["failure_kind"] == "http_429"
    assert result["failure_stage"] == "fetch"
    assert result["retry_after_seconds"] == 180


def test_default_fetcher_applies_transport_response_limit(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(service, "fetch_text", lambda _url, **kwargs: captured.update(kwargs))

    service._default_fetcher(service.DATA_URL)

    assert captured["max_response_bytes"] == service.MAX_RESPONSE_BYTES


def test_refresh_honors_worker_cancel_before_network(isolated_sources: Path) -> None:
    stop_event = threading.Event()
    stop_event.set()

    with pytest.raises(RuntimeError, match="blitz_refresh_cancelled"):
        service.refresh_blitz(
            force=True,
            pointer_output=isolated_sources / "candidates" / "blitz.v2.json",
            fetcher=lambda *_args, **_kwargs: pytest.fail("取消后不应发起网络请求"),
            catalog_binding=_binding(),
            stop_event=stop_event,
        )


def _install_current_blitz(isolated_sources: Path, *, last_success_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {"data": [_row(value) for value in (1001, 1002, 7001, 7002)]}
    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
    )
    pointer = dict(result["pointer"])
    pointer["last_success_at"] = last_success_at
    current = source_runs.source_current_path("blitz")
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(json.dumps(pointer), encoding="utf-8")
    return payload, pointer


@pytest.mark.parametrize("age", (timedelta(hours=3), timedelta(days=7)))
def test_same_marker_reuse_is_version_driven_not_age_driven(
    isolated_sources: Path,
    age: timedelta,
) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    payload, _pointer = _install_current_blitz(
        isolated_sources,
        last_success_at=(now - age).isoformat(),
    )

    result = service.refresh_blitz(
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        now=now,
    )

    assert result["success"] is True
    assert result["reason"] == "not_stale"


def test_missing_parser_revision_reprojects_same_upstream_once(
    isolated_sources: Path,
) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    payload, pointer = _install_current_blitz(
        isolated_sources,
        last_success_at=now.isoformat(),
    )
    manifest_path = source_runs.source_run_dir("blitz", pointer["run_id"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"].pop("parser_revision")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer["manifest_sha256"] = service.sha256_file(manifest_path)
    source_runs.source_current_path("blitz").write_text(json.dumps(pointer), encoding="utf-8")

    repaired = service.refresh_blitz(
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        now=now,
    )
    repaired_manifest = source_runs.load_source_run_manifest("blitz", repaired["run_id"])

    assert repaired["reason"] == "ready"
    assert repaired["run_id"] != pointer["run_id"]
    assert repaired_manifest is not None
    assert repaired_manifest.metadata["parser_revision"] == service.PARSER_REVISION


@pytest.mark.parametrize("last_success_at", ("", "not-a-time"))
def test_missing_or_invalid_blitz_success_time_does_not_override_verified_marker(
    isolated_sources: Path,
    last_success_at: str,
) -> None:
    payload, _pointer = _install_current_blitz(
        isolated_sources,
        last_success_at=last_success_at,
    )

    result = service.refresh_blitz(
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        now=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result["success"] is True
    assert result["reason"] == "not_stale"


def test_expired_blitz_failure_preserves_verified_current_bytes(isolated_sources: Path) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _payload, _pointer = _install_current_blitz(
        isolated_sources,
        last_success_at=(now - timedelta(hours=3)).isoformat(),
    )
    current = source_runs.source_current_path("blitz")
    before = current.read_bytes()

    result = service.refresh_blitz(
        fetcher=FixtureFetcher("timeout", status_code=500, error="timeout"),
        catalog_binding=_binding(),
        now=now,
    )

    assert result["success"] is False
    assert current.read_bytes() == before


def test_force_blitz_refresh_never_reuses_same_marker(isolated_sources: Path) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    payload, pointer = _install_current_blitz(
        isolated_sources,
        last_success_at=now.isoformat(),
    )

    result = service.refresh_blitz(
        force=True,
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        now=now,
    )

    assert result["reason"] == "ready"
    assert result["pointer"]["run_id"] != pointer["run_id"]


def test_conditional_304_reuses_blitz_body_without_new_run(isolated_sources: Path) -> None:
    payload = {"data": [_row(value) for value in (1001, 1002, 7001, 7002)]}
    fetcher = ConditionalFixtureFetcher(
        [
            {
                "status_code": 200,
                "text": json.dumps(payload),
                "response_headers": {"ETag": '"blitz-v1"'},
            },
            {"status_code": 304, "text": "", "response_headers": {}},
        ]
    )
    raw_root = isolated_sources / "raw-responses"
    first = service.refresh_blitz(
        fetcher=fetcher,
        catalog_binding=_binding(),
        raw_cache_root=raw_root,
    )
    current = source_runs.source_current_path("blitz")
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(json.dumps(first["pointer"]), encoding="utf-8")

    second = service.refresh_blitz(
        fetcher=fetcher,
        catalog_binding=_binding(),
        raw_cache_root=raw_root,
    )

    assert second["reason"] == "not_stale"
    assert second["pointer"]["run_id"] == first["run_id"]
    assert fetcher.kwargs[1]["headers"]["If-None-Match"] == '"blitz-v1"'


def test_same_validation_failure_fingerprint_does_not_create_another_run(
    isolated_sources: Path,
) -> None:
    payload = {"data": [_row(1001)]}
    raw_root = isolated_sources / "raw-responses"
    first = service.refresh_blitz(
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        raw_cache_root=raw_root,
    )
    before = sorted(path.name for path in (source_runs.source_root("blitz") / "runs").iterdir())

    second = service.refresh_blitz(
        fetcher=FixtureFetcher(payload),
        catalog_binding=_binding(),
        raw_cache_root=raw_root,
        previous_failure_fingerprint=first["failure_fingerprint"],
    )
    after = sorted(path.name for path in (source_runs.source_root("blitz") / "runs").iterdir())

    assert first["reason"] == "production_coverage_insufficient"
    assert second["reason"] == "validation_unchanged"
    assert second["diagnostics"]["deduplicated"] is True
    assert after == before
