from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from tooling.aramkit_probe.cli import build_parser
from tooling.aramkit_probe.core import (
    DATA_BASE_URL,
    FetchConfig,
    SchemaValidationError,
    TransportResponse,
    compare_latest_runs,
    normalize_detail,
    run_fetch,
)


DATA_PATH_14 = "data/16.14-testhash"
DATA_PATH_13 = "data/16.13-testhash"


def _json_response(payload, *, elapsed_ms: float = 5.0) -> TransportResponse:
    return TransportResponse(200, json.dumps(payload).encode("utf-8"), elapsed_ms)


def _http_response(status: int, *, elapsed_ms: float = 5.0) -> TransportResponse:
    return TransportResponse(status, b"", elapsed_ms, error=f"http_{status}")


def _network_response(kind: str = "timeout") -> TransportResponse:
    return TransportResponse(None, b"", 10.0, error_kind=kind, error=kind)


class FakeTransport:
    def __init__(self, routes: dict[str, TransportResponse | list[TransportResponse]], *, delay: float = 0.0):
        self.routes = {
            url: list(value) if isinstance(value, list) else [value]
            for url, value in routes.items()
        }
        self.calls: list[str] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, url: str, _timeout_seconds: float) -> TransportResponse:
        with self._lock:
            self.calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            queue = self.routes.get(url)
            if not queue:
                response = _http_response(404)
            elif len(queue) > 1:
                response = queue.pop(0)
            else:
                response = queue[0]
        try:
            if self.delay:
                time.sleep(self.delay)
            return response
        finally:
            with self._lock:
                self.active -= 1


def _versions(*, latest_path: str = DATA_PATH_14) -> dict:
    return {
        "latest": "16.14",
        "versions": [
            {
                "version": "16.14",
                "dataPath": latest_path,
                "resourcePath": "resources/ignored",
                "allMatches": 1000,
                "highMatches": 250,
                "dataStartTimeUnixMs": 1,
                "dataEndTimeUnixMs": 2,
                "buildTimeUnixMs": 3,
            },
            {
                "version": "16.13",
                "dataPath": DATA_PATH_13,
                "resourcePath": "resources/ignored-old",
                "allMatches": 900,
                "highMatches": 225,
                "dataStartTimeUnixMs": 1,
                "dataEndTimeUnixMs": 2,
                "buildTimeUnixMs": 3,
            },
        ],
    }


def _ranking(champion_id: int, *, win_rate: float = 0.55) -> dict:
    return {
        "id": champion_id,
        "rank": 1,
        "tier": "S",
        "sampleCount": 100,
        "winRate": win_rate,
        "pickRate": 0.1,
        "blueWinRate": 0.56,
        "redWinRate": 0.54,
    }


def _augment(augment_id: int, *, is_all: bool) -> dict:
    row = {
        "id": augment_id,
        "rank": 1,
        "sampleCount": 50,
        "pickRate": 0.1,
        "winRate": 0.6,
        "blueWinRate": 0.61,
        "redWinRate": 0.59,
    }
    if is_all:
        row.update({"stageAgnostic": False, "availableStages": [1, 2, 3, 4]})
    return row


def _detail(ranking: dict, *, missing_stage: str = "", duplicate: bool = False) -> dict:
    champion = {
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
    }
    stages = {stage: [_augment(1000 + int(stage), is_all=False)] for stage in ("1", "2", "3", "4")}
    if missing_stage:
        stages.pop(missing_stage)
    if duplicate:
        stages["1"].append(dict(stages["1"][0]))
    return {
        "champion": champion,
        "builds": {},
        "items": [],
        "summoners": [],
        "skills": [],
        "augments": {"all": [_augment(1001, is_all=True)], "stages": stages},
        "augmentCombinations": [],
    }


def _urls(dataset: str, data_path: str, champion_ids: list[int]) -> tuple[str, str, dict[int, str]]:
    versions_url = f"{DATA_BASE_URL}/data/versions.json"
    rankings_url = f"{DATA_BASE_URL}/{data_path}/stats/{dataset}/champion-rankings.json"
    details = {
        champion_id: f"{DATA_BASE_URL}/{data_path}/stats/{dataset}/champion-details/{champion_id}.json"
        for champion_id in champion_ids
    }
    return versions_url, rankings_url, details


def _successful_routes(
    *,
    dataset: str = "all",
    data_path: str = DATA_PATH_14,
    champion_ids: list[int] | None = None,
    win_rate: float = 0.55,
) -> dict[str, TransportResponse]:
    champion_ids = champion_ids or [1, 2]
    rankings = [_ranking(champion_id, win_rate=win_rate) for champion_id in champion_ids]
    versions_url, rankings_url, detail_urls = _urls(dataset, data_path, champion_ids)
    routes = {
        versions_url: _json_response(_versions(latest_path=data_path)),
        rankings_url: _json_response({"rows": rankings}),
    }
    routes.update(
        {
            detail_urls[champion_id]: _json_response(_detail(ranking))
            for champion_id, ranking in zip(champion_ids, rankings, strict=True)
        }
    )
    return routes


def test_cli_defaults_to_all_and_concurrency_eight() -> None:
    args = build_parser().parse_args(["fetch"])
    assert args.dataset == "all"
    assert args.concurrency == 8
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch", "--concurrency", "9"])


def test_high_and_explicit_version_build_the_expected_urls(tmp_path: Path) -> None:
    ranking = _ranking(1)
    versions_url, rankings_url, detail_urls = _urls("high", DATA_PATH_13, [1])
    transport = FakeTransport(
        {
            versions_url: _json_response(_versions()),
            rankings_url: _json_response({"rows": [ranking]}),
            detail_urls[1]: _json_response(_detail(ranking)),
        }
    )

    result = run_fetch(
        FetchConfig(dataset="high", version="16.13", output_root=tmp_path),
        transport=transport,
    )

    assert result.complete is True
    assert rankings_url in transport.calls
    assert detail_urls[1] in transport.calls
    assert all("resources/" not in url for url in transport.calls)


def test_complete_run_writes_verified_gzip_artifacts_and_uses_at_most_eight_workers(tmp_path: Path) -> None:
    champion_ids = list(range(1, 13))
    transport = FakeTransport(
        _successful_routes(champion_ids=champion_ids),
        delay=0.01,
    )

    result = run_fetch(FetchConfig(output_root=tmp_path), transport=transport)

    assert result.complete is True
    assert transport.max_active <= 8
    assert result.manifest["coverage"] == {
        "expectedChampions": 12,
        "successfulChampions": 12,
        "missingChampionIds": [],
        "unexpectedChampionIds": [],
        "augmentAllRecords": 12,
        "augmentStageRecords": {"1": 12, "2": 12, "3": 12, "4": 12},
    }
    snapshot_info = result.manifest["artifacts"]["snapshot"]
    snapshot_bytes = (result.run_dir / snapshot_info["path"]).read_bytes()
    assert hashlib.sha256(snapshot_bytes).hexdigest() == snapshot_info["sha256"]
    assert len(snapshot_bytes) == snapshot_info["size"]
    snapshot = json.loads(gzip.decompress(snapshot_bytes))
    assert sorted(snapshot["champions"], key=int) == [str(value) for value in champion_ids]
    assert result.manifest["artifacts"]["raw"]["count"] == len(champion_ids)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda payload: payload["augments"]["stages"].pop("4"), "stages.4"),
        (lambda payload: payload["augments"]["stages"]["1"].append(dict(payload["augments"]["stages"]["1"][0])), "重复"),
        (lambda payload: payload["augments"]["all"][0].update({"winRate": 1.2}), "[0,1]"),
        (lambda payload: payload["champion"].update({"tier": "A"}), "版本混用"),
    ],
)
def test_schema_gate_rejects_incomplete_duplicate_invalid_and_mixed_payloads(mutation, expected_error) -> None:
    ranking = _ranking(1)
    payload = _detail(ranking)
    mutation(payload)
    with pytest.raises(SchemaValidationError, match=expected_error):
        normalize_detail(payload, {
            "id": "1",
            "rank": 1,
            "tier": "S",
            "stats": {
                "sampleCount": 100,
                "winRate": 0.55,
                "pickRate": 0.1,
                "blueWinRate": 0.56,
                "redWinRate": 0.54,
            },
        })


def test_missing_hero_keeps_failed_manifest_and_no_complete_snapshot_claim(tmp_path: Path) -> None:
    routes = _successful_routes(champion_ids=[1, 2])
    _, _, detail_urls = _urls("all", DATA_PATH_14, [1, 2])
    routes[detail_urls[2]] = _http_response(404)

    result = run_fetch(FetchConfig(output_root=tmp_path), transport=FakeTransport(routes))

    assert result.complete is False
    assert result.manifest["complete"] is False
    assert result.manifest["coverage"]["missingChampionIds"] == ["2"]
    assert result.manifest_path.is_file()


@pytest.mark.parametrize("first_failure", [_network_response("timeout"), _http_response(503)])
def test_retryable_detail_failure_uses_one_tail_retry(tmp_path: Path, first_failure: TransportResponse) -> None:
    routes = _successful_routes(champion_ids=[1])
    _, _, detail_urls = _urls("all", DATA_PATH_14, [1])
    routes[detail_urls[1]] = [first_failure, routes[detail_urls[1]]]
    transport = FakeTransport(routes)

    result = run_fetch(FetchConfig(output_root=tmp_path), transport=transport)

    assert result.complete is True
    assert transport.calls.count(detail_urls[1]) == 2


def test_403_opens_circuit_and_skips_queued_details(tmp_path: Path) -> None:
    champion_ids = [1, 2, 3]
    routes = _successful_routes(champion_ids=champion_ids)
    _, _, detail_urls = _urls("all", DATA_PATH_14, champion_ids)
    routes[detail_urls[1]] = _http_response(403)
    transport = FakeTransport(routes)

    result = run_fetch(FetchConfig(concurrency=1, output_root=tmp_path), transport=transport)

    assert result.complete is False
    assert transport.calls.count(detail_urls[1]) == 1
    assert detail_urls[2] not in transport.calls
    assert detail_urls[3] not in transport.calls
    assert result.manifest["requests"]["statusCounts"]["403"] == 1


def test_compare_passes_for_identical_path_and_reports_changes_for_new_path(tmp_path: Path) -> None:
    first = run_fetch(FetchConfig(output_root=tmp_path), transport=FakeTransport(_successful_routes()))
    second = run_fetch(FetchConfig(output_root=tmp_path), transport=FakeTransport(_successful_routes()))
    assert first.complete and second.complete

    same_report = compare_latest_runs(dataset="all", latest=2, output_root=tmp_path)
    assert same_report["passed"] is True
    assert same_report["sameDataPath"] is True
    assert same_report["changedChampionIds"] == []
    assert Path(same_report["reportPath"]).is_file()

    third = run_fetch(
        FetchConfig(output_root=tmp_path),
        transport=FakeTransport(_successful_routes(data_path="data/16.14-newhash", win_rate=0.56)),
    )
    assert third.complete
    changed_report = compare_latest_runs(dataset="all", latest=2, output_root=tmp_path)
    assert changed_report["passed"] is True
    assert changed_report["sameDataPath"] is False
    assert changed_report["changedChampionIds"] == ["1", "2"]


def test_compare_rejects_same_path_with_different_content(tmp_path: Path) -> None:
    assert run_fetch(FetchConfig(output_root=tmp_path), transport=FakeTransport(_successful_routes())).complete
    assert run_fetch(
        FetchConfig(output_root=tmp_path),
        transport=FakeTransport(_successful_routes(win_rate=0.56)),
    ).complete

    report = compare_latest_runs(dataset="all", latest=2, output_root=tmp_path)

    assert report["sameDataPath"] is True
    assert report["passed"] is False
    assert report["changedChampionIds"] == ["1", "2"]
