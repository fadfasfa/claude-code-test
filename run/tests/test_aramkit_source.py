from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from hextech.infrastructure.sources.aramkit import service
from hextech.bootstrap.data_service_runtime import _aramkit_payloads
from hextech.infrastructure.sources.aramkit.schema import (
    SchemaValidationError,
    normalize_detail,
    normalize_rankings,
    resolve_version,
)
from hextech.modules.data import source_runs
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.modules.data.ports.atomic import atomic_write_json


DATA_PATH = "data/16.15-fixture"


def _version(*, data_path: str = DATA_PATH, build_time: int = 1234) -> dict[str, Any]:
    return {
        "latest": "16.15",
        "versions": [
            {
                "version": "16.15",
                "dataPath": data_path,
                "buildTimeUnixMs": build_time,
                "allMatches": 999,
            }
        ],
    }


def _ranking(champion_id: str, *, win_rate: float = 0.51) -> dict[str, Any]:
    return {
        "id": champion_id,
        "rank": int(champion_id),
        "tier": "S",
        "sampleCount": 100,
        "winRate": win_rate,
        "pickRate": 0.1,
        "blueWinRate": 0.52,
        "redWinRate": 0.50,
    }


def _augment(augment_id: int, *, rank: int = 1) -> dict[str, Any]:
    return {
        "id": augment_id,
        "rank": rank,
        "sampleCount": 50,
        "winRate": 0.55,
        "pickRate": 0.2,
        "blueWinRate": 0.56,
        "redWinRate": 0.54,
    }


def _detail(ranking: Mapping[str, Any], augment_id: int) -> dict[str, Any]:
    all_row = {**_augment(augment_id), "stageAgnostic": True, "availableStages": ["1", "2", "3", "4"]}
    return {
        "champion": {
            "id": ranking["id"],
            "rank": ranking["rank"],
            "tier": ranking["tier"],
            "stats": {
                "sampleCount": ranking["sampleCount"],
                "winRate": ranking["winRate"],
                "pickRate": ranking["pickRate"],
                "blueWinRate": ranking["blueWinRate"],
                "redWinRate": ranking["redWinRate"],
            },
        },
        "augments": {
            "all": [all_row],
            "stages": {stage: [_augment(augment_id)] for stage in ("1", "2", "3", "4")},
        },
        "items": {"filtered": ["必须被丢弃"]},
        "builds": {"unfiltered": ["必须被丢弃"]},
        "summoners": ["必须被丢弃"],
    }


def _urls(champion_ids: list[str], *, data_path: str = DATA_PATH) -> tuple[str, str, dict[str, str]]:
    rankings = f"{service.DATA_BASE_URL}/{data_path}/stats/all/champion-rankings.json"
    details = {
        champion_id: f"{service.DATA_BASE_URL}/{data_path}/stats/all/champion-details/{champion_id}.json"
        for champion_id in champion_ids
    }
    return service.VERSIONS_URL, rankings, details


class FixtureFetcher:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []
        self.counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def __call__(self, url: str, **_: object) -> object:
        with self._lock:
            self.calls.append(url)
            self.counts[url] += 1
            call = self.counts[url]
        response = self.responses[url]
        if isinstance(response, list):
            return response[min(call - 1, len(response) - 1)]
        return response


class ConditionalFixtureFetcher(FixtureFetcher):
    def __init__(self, responses: Mapping[str, object]) -> None:
        super().__init__(responses)
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, url: str, **kwargs: object) -> object:
        self.kwargs.append(dict(kwargs))
        return super().__call__(url, **kwargs)


@pytest.fixture
def isolated_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    return tmp_path


def _binding(
    champion_ids: list[str],
    augment_ids: set[int] | None = None,
    *,
    compatible_extra_augment_ids: set[int] | None = None,
) -> service.CatalogBinding:
    return service.CatalogBinding(
        generation_id="catalog-fixture",
        content_sha256="c" * 64,
        champion_ids=frozenset(champion_ids),
        augment_ids=frozenset(augment_ids or {10, 20, 30}),
        compatible_extra_augment_ids=frozenset(compatible_extra_augment_ids or set()),
    )


def _happy_fetcher(champion_ids: list[str], *, augment_id: int = 10) -> FixtureFetcher:
    rankings = [_ranking(champion_id) for champion_id in champion_ids]
    versions_url, rankings_url, detail_urls = _urls(champion_ids)
    responses: dict[str, object] = {
        versions_url: _version(),
        rankings_url: {"rows": rankings},
    }
    responses.update(
        {detail_urls[champion_id]: _detail(ranking, augment_id) for champion_id, ranking in zip(champion_ids, rankings, strict=True)}
    )
    return FixtureFetcher(responses)


def test_version_schema_no_longer_requires_removed_data_window_fields() -> None:
    resolved = resolve_version(_version())

    assert resolved == {
        "version": "16.15",
        "dataPath": DATA_PATH,
        "allMatches": 999,
        "buildTimeUnixMs": 1234,
    }


def test_429_retry_after_is_preserved_in_source_result(isolated_sources: Path) -> None:
    response = SimpleNamespace(
        text="",
        status_code=429,
        error="http_429",
        error_kind="http_429",
        response_headers={"Retry-After": "120"},
    )

    result = service.refresh_aramkit(
        fetcher=lambda *_args, **_kwargs: response,
        catalog_binding=_binding(["1"]),
    )

    assert result["success"] is False
    assert result["failure_kind"] == "http_429"
    assert result["failure_stage"] == "fetch"
    assert result["retry_after_seconds"] == 120


def test_default_fetcher_applies_transport_response_limit(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(service, "fetch_text", lambda _url, **kwargs: captured.update(kwargs))

    service._default_fetcher(service.VERSIONS_URL)

    assert captured["max_response_bytes"] == service.MAX_RESPONSE_BYTES


def test_schema_rejects_duplicate_invalid_rate_and_empty_stage() -> None:
    ranking = _ranking("1")
    duplicate = _detail(ranking, 10)
    duplicate["augments"]["all"].append(dict(duplicate["augments"]["all"][0]))
    with pytest.raises(SchemaValidationError, match="重复海克斯"):
        normalize_detail(duplicate, normalize_rankings({"rows": [ranking]})[0])

    invalid_rate = _detail(ranking, 10)
    invalid_rate["augments"]["all"][0]["winRate"] = 1.1
    with pytest.raises(SchemaValidationError, match=r"\[0,1\]"):
        normalize_detail(invalid_rate, normalize_rankings({"rows": [ranking]})[0])

    empty_stage = _detail(ranking, 10)
    empty_stage["augments"]["stages"]["4"] = []
    with pytest.raises(SchemaValidationError, match="不能为空"):
        normalize_detail(empty_stage, normalize_rankings({"rows": [ranking]})[0])


def test_happy_path_writes_hash_bound_files_without_raw_payload(isolated_sources: Path) -> None:
    fetcher = _happy_fetcher(["1", "2"])
    pointer_output = isolated_sources / "candidates" / "aramkit.v2.json"

    result = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["1", "2"]),
        pointer_output=pointer_output,
    )

    assert result["success"] is True
    assert result["reason"] == "ready"
    assert pointer_output.is_file()
    index = service.validate_scoped_stats_artifact(result["pointer"])
    assert index["champion_count"] == 2
    assert index["record_count"] == 2
    run_dir = source_runs.source_run_dir("aramkit", result["run_id"])
    assert not (run_dir / "raw").exists()
    child = json.loads((run_dir / "scoped_stats" / "champions" / "1.json").read_text(encoding="utf-8"))
    assert set(child) == {"schema_version", "source", "dataset", "version", "data_path", "champion", "all", "stages"}
    assert "必须被丢弃" not in json.dumps(child, ensure_ascii=False)
    assert source_runs.load_source_current("aramkit") == {}


def test_generation_adapter_uses_catalog_display_fields_and_local_rank(isolated_sources: Path) -> None:
    catalog = load_active_catalog()
    fetcher = _happy_fetcher(["266"], augment_id=1001)
    result = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["266"], {1001}),
    )

    champions, details = _aramkit_payloads(result["pointer"], catalog=catalog)

    assert champions[0]["id"] == "266"
    assert champions[0]["name"] == "暗裔剑魔"
    card = details["暗裔剑魔"]["augments"][0]
    assert card["id"] == "1001"
    assert card["rank"] == 1
    assert card["source_rank"] == 1
    assert card["winrate"] == pytest.approx(0.55)
    assert card["pickrate"] == pytest.approx(0.2)
    assert card["sample_count"] == 50
    assert card["海克斯名称"]
    assert card["海克斯阶级"]
    assert card["icon"]


def test_same_marker_reuses_verified_current_without_fetching_rankings(isolated_sources: Path) -> None:
    first_fetcher = _happy_fetcher(["1"])
    first = service.refresh_aramkit(fetcher=first_fetcher, catalog_binding=_binding(["1"]))
    atomic_write_json(source_runs.source_current_path("aramkit"), first["pointer"])
    marker_only = FixtureFetcher({service.VERSIONS_URL: _version()})

    result = service.refresh_aramkit(fetcher=marker_only, catalog_binding=_binding(["1"]))

    assert result["success"] is True
    assert result["reason"] == "not_stale"
    assert marker_only.calls == [service.VERSIONS_URL]


def test_versions_304_reuses_verified_response_without_redownloading_dataset(
    isolated_sources: Path,
) -> None:
    happy = _happy_fetcher(["1"])
    responses = dict(happy.responses)
    responses[service.VERSIONS_URL] = [
        {
            "status_code": 200,
            "text": json.dumps(_version()),
            "response_headers": {"ETag": '"versions-v1"'},
        },
        {"status_code": 304, "text": "", "response_headers": {}},
        {"status_code": 304, "text": "", "response_headers": {}},
    ]
    fetcher = ConditionalFixtureFetcher(responses)
    raw_root = isolated_sources / "raw-responses"
    first = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["1"]),
        raw_cache_root=raw_root,
    )
    atomic_write_json(source_runs.source_current_path("aramkit"), first["pointer"])

    second = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["1"]),
        raw_cache_root=raw_root,
    )

    assert second["reason"] == "not_stale"
    assert fetcher.counts[service.VERSIONS_URL] == 3
    assert fetcher.counts[_urls(["1"])[1]] == 1
    version_calls = [
        kwargs for url, kwargs in zip(fetcher.calls, fetcher.kwargs, strict=True)
        if url == service.VERSIONS_URL
    ]
    assert version_calls[1]["headers"]["If-None-Match"] == '"versions-v1"'
    assert version_calls[2]["headers"]["If-None-Match"] == '"versions-v1"'


def test_damaged_current_is_rebuilt_from_verified_raw_cache(
    isolated_sources: Path,
) -> None:
    raw_root = isolated_sources / "raw-responses"
    first = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]),
        catalog_binding=_binding(["1"]),
        raw_cache_root=raw_root,
    )
    atomic_write_json(source_runs.source_current_path("aramkit"), first["pointer"])
    index_path = source_runs.source_run_artifact_path(
        "aramkit",
        first["run_id"],
        first["pointer"]["artifact"]["relative_path"],
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    child = index_path.parent / index["files"][0]["relative_path"]
    child.write_text("damaged", encoding="utf-8")
    fetcher = FixtureFetcher({service.VERSIONS_URL: _version()})

    second = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["1"]),
        raw_cache_root=raw_root,
    )

    assert second["success"] is True and second["reason"] == "ready"
    assert second["run_id"] != first["run_id"]
    assert fetcher.calls == [service.VERSIONS_URL, service.VERSIONS_URL]


@pytest.mark.parametrize("age", [timedelta(hours=5), timedelta(days=7)])
def test_same_marker_reuse_is_version_driven_not_age_driven(
    isolated_sources: Path,
    age: timedelta,
) -> None:
    first = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]),
        catalog_binding=_binding(["1"]),
    )
    current = dict(first["pointer"])
    atomic_write_json(source_runs.source_current_path("aramkit"), current)
    completed = datetime.fromisoformat(str(current["last_success_at"]).replace("Z", "+00:00"))
    fetcher = FixtureFetcher({service.VERSIONS_URL: _version()})

    checks = 42 if age == timedelta(days=7) else 1
    for step in range(1, checks + 1):
        result = service.refresh_aramkit(
            fetcher=fetcher,
            catalog_binding=_binding(["1"]),
            now=completed + age * step / checks,
        )
        assert result["success"] is True
        assert result["reason"] == "not_stale"
        assert result["pointer"]["run_id"] == first["run_id"]
    assert fetcher.calls == [service.VERSIONS_URL] * checks


def test_missing_pointer_success_time_falls_back_to_manifest_completion(isolated_sources: Path) -> None:
    first = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]),
        catalog_binding=_binding(["1"]),
    )
    current = {**first["pointer"], "last_success_at": ""}
    atomic_write_json(source_runs.source_current_path("aramkit"), current)
    manifest = source_runs.load_source_run_manifest("aramkit", first["run_id"])
    assert manifest is not None
    completed = datetime.fromisoformat(manifest.completed_at.replace("Z", "+00:00"))
    marker_only = FixtureFetcher({service.VERSIONS_URL: _version()})

    result = service.refresh_aramkit(
        fetcher=marker_only,
        catalog_binding=_binding(["1"]),
        now=completed + timedelta(hours=5),
    )

    assert result["reason"] == "not_stale"
    assert marker_only.calls == [service.VERSIONS_URL]


def test_invalid_pointer_success_time_does_not_override_verified_marker(isolated_sources: Path) -> None:
    first = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]),
        catalog_binding=_binding(["1"]),
    )
    current = {**first["pointer"], "last_success_at": "not-a-time"}
    atomic_write_json(source_runs.source_current_path("aramkit"), current)
    fetcher = FixtureFetcher({service.VERSIONS_URL: _version()})

    result = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(["1"]),
        now=datetime.fromisoformat(str(first["pointer"]["last_success_at"]).replace("Z", "+00:00")),
    )

    assert result["success"] is True
    assert result["reason"] == "not_stale"
    assert fetcher.calls == [service.VERSIONS_URL]


def test_old_same_marker_skips_invalid_full_payload_and_preserves_current(isolated_sources: Path) -> None:
    first = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]),
        catalog_binding=_binding(["1"]),
    )
    current_path = source_runs.source_current_path("aramkit")
    atomic_write_json(current_path, first["pointer"])
    before = current_path.read_bytes()
    completed = datetime.fromisoformat(str(first["pointer"]["last_success_at"]).replace("Z", "+00:00"))
    _versions_url, rankings_url, _detail_urls = _urls(["1"])
    invalid_rankings = FixtureFetcher(
        {
            service.VERSIONS_URL: _version(),
            rankings_url: {"rows": []},
        }
    )

    result = service.refresh_aramkit(
        fetcher=invalid_rankings,
        catalog_binding=_binding(["1"]),
        now=completed + timedelta(hours=5, seconds=1),
    )

    assert result["success"] is True
    assert result["reason"] == "not_stale"
    assert invalid_rankings.calls == [service.VERSIONS_URL]
    assert current_path.read_bytes() == before


def test_same_validation_input_is_suppressed_before_dataset_download(
    isolated_sources: Path,
) -> None:
    _versions_url, rankings_url, _detail_urls = _urls(["1"])
    first = service.refresh_aramkit(
        force=True,
        fetcher=FixtureFetcher(
            {
                service.VERSIONS_URL: _version(),
                rankings_url: {"rows": []},
            }
        ),
        catalog_binding=_binding(["1"]),
    )
    marker_only = FixtureFetcher({service.VERSIONS_URL: _version()})

    second = service.refresh_aramkit(
        force=True,
        fetcher=marker_only,
        catalog_binding=_binding(["1"]),
        previous_failure_fingerprint=first["failure_fingerprint"],
    )

    assert first["reason"] == "schema_changed"
    assert first["failure_fingerprint"]
    assert second["reason"] == "validation_unchanged"
    assert second["failure_fingerprint"] == first["failure_fingerprint"]
    assert marker_only.calls == [service.VERSIONS_URL]


def test_unknown_champion_or_augment_rejects_candidate_and_preserves_current(isolated_sources: Path) -> None:
    good = service.refresh_aramkit(fetcher=_happy_fetcher(["1"]), catalog_binding=_binding(["1"]))
    current_path = source_runs.source_current_path("aramkit")
    atomic_write_json(current_path, good["pointer"])
    before = current_path.read_bytes()

    unknown_champion = service.refresh_aramkit(
        force=True,
        fetcher=_happy_fetcher(["2"]),
        catalog_binding=_binding(["1"]),
    )
    assert unknown_champion["success"] is False
    assert unknown_champion["reason"] == "catalog_binding_failed"

    unknown_augment = service.refresh_aramkit(
        force=True,
        fetcher=_happy_fetcher(["1"], augment_id=999),
        catalog_binding=_binding(["1"], {10}),
    )
    assert unknown_augment["success"] is False
    assert unknown_augment["reason"] == "catalog_binding_failed"
    assert current_path.read_bytes() == before


def test_blocked_adoption_extra_augment_is_filtered_from_active_catalog_artifact(
    isolated_sources: Path,
) -> None:
    fetcher = _happy_fetcher(["1"], augment_id=10)
    _versions_url, _rankings_url, detail_urls = _urls(["1"])
    detail = fetcher.responses[detail_urls["1"]]
    assert isinstance(detail, dict)
    extra_all = {
        **_augment(999, rank=2),
        "stageAgnostic": True,
        "availableStages": ["1", "2", "3", "4"],
    }
    detail["augments"]["all"].append(extra_all)
    for rows in detail["augments"]["stages"].values():
        rows.append(_augment(999, rank=2))

    result = service.refresh_aramkit(
        force=True,
        fetcher=fetcher,
        catalog_binding=_binding(
            ["1"],
            {10},
            compatible_extra_augment_ids={999},
        ),
    )

    assert result["success"] is True
    assert result["compatibility_filtered_augment_ids"] == [999]
    run_dir = source_runs.source_run_dir("aramkit", result["run_id"])
    child = json.loads(
        (run_dir / "scoped_stats" / "champions" / "1.json").read_text(encoding="utf-8")
    )
    assert {row["id"] for row in child["all"]} == {"10"}
    assert all({row["id"] for row in rows} == {"10"} for rows in child["stages"].values())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["compatibility_filtered_augment_ids"] == [999]


def test_timeout_gets_one_tail_retry_and_403_opens_circuit(isolated_sources: Path) -> None:
    rankings = [_ranking(str(index)) for index in range(1, 5)]
    versions_url, rankings_url, detail_urls = _urls([str(index) for index in range(1, 5)])
    responses: dict[str, object] = {
        versions_url: _version(),
        rankings_url: {"rows": rankings},
        detail_urls["1"]: [(None, b"", "timeout"), _detail(rankings[0], 10)],
    }
    responses.update({detail_urls[str(index)]: _detail(rankings[index - 1], 10) for index in range(2, 5)})
    retrying = FixtureFetcher(responses)

    result = service.refresh_aramkit(
        fetcher=retrying,
        catalog_binding=_binding(["1", "2", "3", "4"]),
        concurrency=4,
    )

    assert result["success"] is True
    assert retrying.counts[detail_urls["1"]] == 2

    blocked_responses = {
        versions_url: _version(),
        rankings_url: {"rows": rankings},
        **{detail_urls[str(index)]: _detail(rankings[index - 1], 10) for index in range(1, 5)},
    }
    blocked_responses[detail_urls["1"]] = (403, b"forbidden", "http_403")
    blocked = FixtureFetcher(blocked_responses)
    blocked_result = service.refresh_aramkit(
        force=True,
        fetcher=blocked,
        catalog_binding=_binding(["1", "2", "3", "4"]),
        concurrency=1,
    )

    assert blocked_result["success"] is False
    assert blocked_result["reason"] == "blocked"
    assert sum(blocked.counts[url] for url in detail_urls.values()) == 1


def test_top_level_timeout_retries_once_but_403_fails_immediately(isolated_sources: Path) -> None:
    rankings = [_ranking("1")]
    versions_url, rankings_url, detail_urls = _urls(["1"])
    retrying = FixtureFetcher(
        {
            versions_url: [(None, b"", "timeout"), _version()],
            rankings_url: {"rows": rankings},
            detail_urls["1"]: _detail(rankings[0], 10),
        }
    )

    result = service.refresh_aramkit(
        fetcher=retrying,
        catalog_binding=_binding(["1"]),
    )

    assert result["success"] is True
    # 首次 timeout、一次有限重试，以及成功发布前的 marker 漂移复核。
    assert retrying.counts[versions_url] == 3

    blocked = FixtureFetcher({versions_url: (403, b"forbidden", "http_403")})
    blocked_result = service.refresh_aramkit(
        force=True,
        fetcher=blocked,
        catalog_binding=_binding(["1"]),
    )
    assert blocked_result["success"] is False
    assert blocked_result["reason"] == "http_403"
    assert blocked.counts[versions_url] == 1


def test_marker_drift_and_response_budget_fail_closed(isolated_sources: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = _happy_fetcher(["1"])
    fetcher.responses[service.VERSIONS_URL] = [_version(), _version(data_path="data/16.15-new", build_time=5678)]

    drift = service.refresh_aramkit(fetcher=fetcher, catalog_binding=_binding(["1"]))

    assert drift["success"] is False
    assert drift["reason"] == "marker_drift"

    monkeypatch.setattr(service, "MAX_RESPONSE_BYTES", 10)
    over_budget = service.refresh_aramkit(
        fetcher=FixtureFetcher({service.VERSIONS_URL: _version()}),
        catalog_binding=_binding(["1"]),
    )
    assert over_budget["success"] is False
    assert over_budget["reason"] == "response_too_large"

    monkeypatch.setattr(service, "MAX_RESPONSE_BYTES", 1024 * 1024)
    monkeypatch.setattr(service, "MAX_TOTAL_BYTES", 10)
    total_budget = service.refresh_aramkit(
        fetcher=FixtureFetcher({service.VERSIONS_URL: _version()}),
        catalog_binding=_binding(["1"]),
    )
    assert total_budget["success"] is False
    assert total_budget["reason"] == "download_budget_exceeded"


def test_concurrency_hard_limit_and_child_tamper_detection(isolated_sources: Path) -> None:
    with pytest.raises(ValueError, match="1..4"):
        service.refresh_aramkit(
            fetcher=_happy_fetcher(["1"]),
            catalog_binding=_binding(["1"]),
            concurrency=9,
        )

    result = service.refresh_aramkit(fetcher=_happy_fetcher(["1"]), catalog_binding=_binding(["1"]))
    index = service.validate_scoped_stats_artifact(result["pointer"])
    child = source_runs.source_run_dir("aramkit", result["run_id"]) / "scoped_stats" / index["files"][0]["relative_path"]
    child.write_text("{}", encoding="utf-8")

    with pytest.raises(source_runs.SourceRunValidationError, match="子文件哈希或大小"):
        service.validate_scoped_stats_artifact(result["pointer"])


def test_tail_retry_executor_never_exceeds_two_workers(isolated_sources: Path) -> None:
    champion_ids = ["1", "2", "3", "4"]
    rankings = [_ranking(item) for item in champion_ids]
    versions_url, rankings_url, detail_urls = _urls(champion_ids)
    calls: Counter[str] = Counter()
    active = 0
    max_retry_active = 0
    lock = threading.Lock()

    def fetcher(url: str, **_: object) -> object:
        nonlocal active, max_retry_active
        if url == versions_url:
            return _version()
        if url == rankings_url:
            return {"rows": rankings}
        champion_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        with lock:
            calls[url] += 1
            attempt = calls[url]
            if attempt == 2:
                active += 1
                max_retry_active = max(max_retry_active, active)
        if attempt == 1:
            return (500, b"server error", "http_5xx")
        time.sleep(0.03)
        try:
            return _detail(rankings[int(champion_id) - 1], 10)
        finally:
            with lock:
                active -= 1

    result = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(champion_ids),
        concurrency=4,
    )

    assert result["success"] is True
    assert max_retry_active == service.RETRY_CONCURRENCY


def test_default_detail_executor_uses_four_workers(isolated_sources: Path) -> None:
    champion_ids = [str(index) for index in range(1, 9)]
    rankings = [_ranking(item) for item in champion_ids]
    versions_url, rankings_url, _detail_urls = _urls(champion_ids)
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_batch_ready = threading.Event()

    def fetcher(url: str, **_: object) -> object:
        nonlocal active, max_active
        if url == versions_url:
            return _version()
        if url == rankings_url:
            return {"rows": rankings}
        champion_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == service.DEFAULT_CONCURRENCY:
                first_batch_ready.set()
        try:
            assert first_batch_ready.wait(2.0), "完整第一批worker未并发启动"
            return _detail(rankings[int(champion_id) - 1], 10)
        finally:
            with lock:
                active -= 1

    result = service.refresh_aramkit(
        fetcher=fetcher,
        catalog_binding=_binding(champion_ids),
    )

    assert result["success"] is True
    assert max_active == service.DEFAULT_CONCURRENCY
