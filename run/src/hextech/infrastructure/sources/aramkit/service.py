"""ARAMKit ``dataset=all`` 的静态抓取、Catalog 绑定与候选发布。

本模块只走既有 ``scrapling_client.fetch_text`` 静态 HTTP 路径。它不保存上游
raw，不独立切换正式 current；完整候选由 cohort promotion 决定是否晋升。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from hextech.contracts import ItemOutcome, SourceHealth, SourceRunManifestV2, utc_now_iso
from hextech.contracts.models import FailureKind
from hextech.infrastructure.transport.scrapling_client import fetch_text
from hextech.infrastructure.persistence.raw_responses import (
    RawResponseCache,
    RawResponseIntegrityError,
)
from hextech.modules.acquisition.champion_downloads import ChampionDownloads, DownloadContext
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import (
    SourceRunValidationError,
    build_artifact_descriptor,
    publish_source_run,
    source_run_artifact_path,
    source_run_dir,
    write_run_diagnostics,
)

from .schema import (
    SchemaValidationError,
    decode_object,
    normalize_detail,
    normalize_rankings,
    resolve_version,
    version_marker,
)
from .reuse import load_reusable_current
from .http_response import _Response, _coerce_response
from .failure import duplicate_validation_result, failure_fields, validation_input_fingerprint
from .download_budget import ByteBudget as _ByteBudget
from .conditional import with_conditional_versions
from .catalog_binding import (
    AramkitRefreshError,
    CatalogBinding,
    filter_detail_to_catalog as _filter_detail_to_catalog,
)


DATA_BASE_URL = "https://data.aramkit.com"
VERSIONS_URL = f"{DATA_BASE_URL}/data/versions.json"
DATASET = "all"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 4
RETRY_CONCURRENCY = 2
TIMEOUT_MS = 15_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

Fetcher = Callable[..., object]

@dataclass(frozen=True)
class _DetailResult:
    champion_id: str
    response: _Response | None
    normalized: Mapping[str, Any] | None = None
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.normalized is not None and not self.reason

    @property
    def retryable(self) -> bool:
        return self.response is not None and self.response.retryable


def _default_fetcher(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, object] | None = None,
    **_: object,
) -> object:
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    return fetch_text(
        url,
        timeout_ms=TIMEOUT_MS,
        max_attempts=1,
        headers=request_headers,
        params=dict(params or {}),
        caller="aramkit",
        max_response_bytes=MAX_RESPONSE_BYTES,
    )


def _fetch(fetcher: Fetcher, url: str, budget: _ByteBudget) -> _Response:
    raw = fetcher(
        url,
        timeout_ms=TIMEOUT_MS,
        max_attempts=1,
        headers={"Accept": "application/json"},
        caller="aramkit",
    )
    response = _coerce_response(url, raw)
    budget.reserve(0 if response.from_cache else len(response.body))
    return response


def _require_json_response(fetcher: Fetcher, url: str, budget: _ByteBudget, *, context: str) -> dict[str, Any]:
    """顶层 JSON 对瞬时网络失败重试一次；阻断、4xx 与 schema 错误立即失败。"""

    response = _fetch(fetcher, url, budget)
    if response.failure_kind is not None and response.retryable:
        time.sleep(0.5)
        response = _fetch(fetcher, url, budget)
    if response.failure_kind is not None:
        raise AramkitRefreshError(
            response.failure_kind.value,
            f"{context} 请求失败：status={response.status_code} kind={response.failure_kind.value}",
            response=response,
        )
    try:
        return decode_object(response.body, context=context)
    except SchemaValidationError as exc:
        raise AramkitRefreshError("schema_changed", str(exc), response=response) from exc


def _version_payload(fetcher: Fetcher, budget: _ByteBudget) -> dict[str, Any]:
    return resolve_version(_require_json_response(fetcher, VERSIONS_URL, budget, context="versions"))


def probe_aramkit_upstream_marker(
    *,
    fetcher: Fetcher | None = None,
    conditional_cache_root: Path | None = None,
) -> dict[str, Any]:
    """轻量读取公开 versions marker，不创建 source run。"""

    budget = _ByteBudget(per_response=MAX_RESPONSE_BYTES, total=MAX_TOTAL_BYTES)
    fetch = with_conditional_versions(
        fetcher or _default_fetcher,
        conditional_cache_root,
        versions_url=VERSIONS_URL,
    )
    return version_marker(_version_payload(fetch, budget))


def _detail_url(version: Mapping[str, Any], champion_id: str) -> str:
    return (
        f"{DATA_BASE_URL}/{version['dataPath']}/stats/{DATASET}/"
        f"champion-details/{champion_id}.json"
    )


def _fetch_detail(
    fetcher: Fetcher,
    budget: _ByteBudget,
    version: Mapping[str, Any],
    ranking: Mapping[str, Any],
    stop_event: threading.Event,
) -> _DetailResult:
    champion_id = str(ranking["id"])
    if stop_event.is_set():
        return _DetailResult(champion_id, None, reason="circuit_open")
    try:
        response = _fetch(fetcher, _detail_url(version, champion_id), budget)
    except AramkitRefreshError as exc:
        stop_event.set()
        return _DetailResult(champion_id, None, reason=exc.reason)
    if response.blocking:
        stop_event.set()
    failure = response.failure_kind
    if failure is not None:
        return _DetailResult(champion_id, response, reason=failure.value)
    try:
        payload = decode_object(response.body, context=f"champion-detail[{champion_id}]")
        return _DetailResult(champion_id, response, normalized=normalize_detail(payload, ranking))
    except SchemaValidationError as exc:
        return _DetailResult(champion_id, response, reason=f"schema_changed:{exc}")


def _detail_pass(
    fetcher: Fetcher,
    budget: _ByteBudget,
    version: Mapping[str, Any],
    rankings: list[dict[str, Any]],
    *,
    concurrency: int,
    stop_event: threading.Event,
    context: Callable[[], DownloadContext] | None = None,
    on_detail: Callable[[str, _DetailResult], None] | None = None,
) -> list[_DetailResult]:
    if concurrency <= 0 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"ARAMKit concurrency 必须在 1..{MAX_CONCURRENCY}")
    by_id = {str(row["id"]): row for row in rankings}

    def fetch_one(champion_id: str) -> _DetailResult:
        return _fetch_detail(fetcher, budget, version, by_id[champion_id], stop_event)

    return list(ChampionDownloads[_DetailResult](context or DownloadContext, concurrency=concurrency).run(
        list(by_id), fetch_one, stop=stop_event, completed=on_detail,
    ).values())


def _all_augment_ids(detail: Mapping[str, Any]) -> set[int]:
    augments = detail.get("augments")
    if not isinstance(augments, Mapping):
        return set()
    rows: list[object] = list(augments.get("all") or [])
    stages = augments.get("stages")
    if isinstance(stages, Mapping):
        for stage_rows in stages.values():
            if isinstance(stage_rows, list):
                rows.extend(stage_rows)
    return {int(item["id"]) for item in rows if isinstance(item, Mapping)}


def _project_augment(row: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        "id": str(row["id"]),
        "source_rank": int(row["rank"]),
        "sample_count": int(row["sampleCount"]),
        "win_rate": float(row["winRate"]),
        "pick_rate": float(row["pickRate"]),
        "blue_win_rate": float(row["blueWinRate"]),
        "red_win_rate": float(row["redWinRate"]),
    }
    if "stageAgnostic" in row:
        projected["stage_agnostic"] = bool(row["stageAgnostic"])
        projected["available_stages"] = list(row["availableStages"])
    return projected


def _project_champion(version: Mapping[str, Any], detail: Mapping[str, Any]) -> dict[str, Any]:
    stats = detail["stats"]
    augments = detail["augments"]
    return {
        "schema_version": 1,
        "source": "aramkit",
        "dataset": DATASET,
        "version": str(version["version"]),
        "data_path": str(version["dataPath"]),
        "champion": {
            "id": str(detail["id"]),
            "source_rank": int(detail["rank"]),
            "source_tier": str(detail["tier"]),
            "sample_count": int(stats["sampleCount"]),
            "win_rate": float(stats["winRate"]),
            "pick_rate": float(stats["pickRate"]),
            "blue_win_rate": float(stats["blueWinRate"]),
            "red_win_rate": float(stats["redWinRate"]),
        },
        "all": [_project_augment(item) for item in augments["all"]],
        "stages": {
            stage: [_project_augment(item) for item in rows]
            for stage, rows in augments["stages"].items()
        },
    }


def _sha256_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _write_artifact(
    run_id: str,
    version: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
) -> tuple[Path, int]:
    run_dir = source_run_dir("aramkit", run_id)
    scoped_root = run_dir / "scoped_stats"
    files: list[dict[str, Any]] = []
    total_records = 0
    for champion_id in sorted(details, key=int):
        payload = _project_champion(version, details[champion_id])
        relative = Path("champions") / f"{champion_id}.json"
        path = scoped_root / relative
        atomic_write_json(path, payload, ensure_ascii=False, separators=(",", ":"))
        sha256, size = _sha256_size(path)
        record_count = len(payload["all"])
        total_records += record_count
        files.append(
            {
                "relative_path": relative.as_posix(),
                "champion_id": champion_id,
                "size": size,
                "sha256": sha256,
                "record_count": record_count,
                "data_path": str(version["dataPath"]),
            }
        )
    index = {
        "schema_version": 1,
        "source": "aramkit",
        "dataset": DATASET,
        "version": str(version["version"]),
        "data_path": str(version["dataPath"]),
        "marker": version_marker(version),
        "champion_count": len(files),
        "record_count": total_records,
        "files": files,
    }
    index_path = scoped_root / "manifest.json"
    atomic_write_json(index_path, index, ensure_ascii=False, indent=2)
    return index_path, total_records


def validate_scoped_stats_artifact(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """验证索引及所有逐英雄文件，避免只校验外层 manifest 漏掉子文件篡改。"""

    if str(pointer.get("source") or "") != "aramkit":
        raise SourceRunValidationError("ARAMKit pointer source 无效")
    artifact = pointer.get("artifact")
    if not isinstance(artifact, Mapping) or str(artifact.get("role") or "") != "scoped_stats":
        raise SourceRunValidationError("ARAMKit pointer 缺少 scoped_stats artifact")
    run_id = str(pointer.get("run_id") or "")
    index_path = source_run_artifact_path("aramkit", run_id, str(artifact.get("relative_path") or ""))
    expected_sha = str(artifact.get("sha256") or "")
    sha256, size = _sha256_size(index_path)
    if sha256 != expected_sha or size != int(artifact.get("size", -1)):
        raise SourceRunValidationError("ARAMKit scoped_stats 索引哈希或大小校验失败")
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRunValidationError(f"ARAMKit scoped_stats 索引无效：{exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise SourceRunValidationError("ARAMKit scoped_stats 索引 schema 无效")
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != int(payload.get("champion_count", -1)):
        raise SourceRunValidationError("ARAMKit scoped_stats 文件索引不完整")
    scoped_root = index_path.parent.resolve()
    seen: set[str] = set()
    records = 0
    for item in files:
        if not isinstance(item, Mapping):
            raise SourceRunValidationError("ARAMKit scoped_stats 文件描述无效")
        champion_id = str(item.get("champion_id") or "")
        relative = str(item.get("relative_path") or "").replace("\\", "/")
        target = (scoped_root / relative).resolve()
        if not champion_id or champion_id in seen or scoped_root not in target.parents or not target.is_file():
            raise SourceRunValidationError(f"ARAMKit scoped_stats 子文件缺失或越界：{relative}")
        child_sha, child_size = _sha256_size(target)
        if child_sha != str(item.get("sha256") or "") or child_size != int(item.get("size", -1)):
            raise SourceRunValidationError(f"ARAMKit scoped_stats 子文件哈希或大小校验失败：{relative}")
        seen.add(champion_id)
        records += int(item.get("record_count", 0))
    if records != int(payload.get("record_count", -1)) or records != int(artifact.get("record_count", -1)):
        raise SourceRunValidationError("ARAMKit scoped_stats record_count 不一致")
    return dict(payload)


def _failure_outcome(champion_id: str, result: _DetailResult | None, *, reason: str = "") -> ItemOutcome:
    failure = result.response.failure_kind if result and result.response else FailureKind.INVALID_PAYLOAD
    detail_reason = reason or (result.reason if result else "missing_outcome")
    if detail_reason.startswith("schema_changed"):
        failure = FailureKind.SCHEMA_CHANGED
    return ItemOutcome(
        item_id=champion_id,
        state="failed",
        stage="detail",
        failure_kind=failure,
        attempt=result.response.attempt() if result and result.response else None,
        details={"reason": detail_reason},
    )


def _write_failure(
    *,
    run_id: str,
    binding: CatalogBinding,
    started_at: str,
    rankings: list[dict[str, Any]],
    results: Mapping[str, _DetailResult],
    reason: str,
    marker: Mapping[str, Any] | None,
    bytes_used: int,
    response: _Response | None = None,
    failure_fingerprint: str = "",
) -> dict[str, Any]:
    outcomes: list[ItemOutcome] = []
    if rankings:
        for ranking in rankings:
            champion_id = str(ranking["id"])
            result = results.get(champion_id)
            if result is not None and result.success and reason not in {"marker_drift", "catalog_binding_failed"}:
                outcomes.append(
                    ItemOutcome(
                        item_id=champion_id,
                        state="success",
                        stage="detail",
                        record_count=len(result.normalized["augments"]["all"]),  # type: ignore[index]
                        attempt=result.response.attempt() if result.response else None,
                    )
                )
            else:
                outcomes.append(_failure_outcome(champion_id, result, reason=reason if result and result.success else ""))
    else:
        outcomes.append(
            ItemOutcome(
                item_id="upstream",
                state="failed",
                stage="handshake",
                failure_kind=FailureKind.SCHEMA_CHANGED if reason == "schema_changed" else FailureKind.INVALID_PAYLOAD,
                details={"reason": reason},
            )
        )
    successful = sum(item.state == "success" for item in outcomes)
    responses = [item.response for item in results.values() if item and item.response]
    if response is not None:
        responses.append(response)
    manifest = SourceRunManifestV2(
        source="aramkit",
        run_id=run_id,
        catalog_generation_id=binding.generation_id,
        catalog_sha256=binding.content_sha256,
        health=SourceHealth.FAILED,
        started_at=started_at,
        completed_at=utc_now_iso(),
        expected_items=len(outcomes),
        successful_items=successful,
        confirmed_empty_items=0,
        failed_items=len(outcomes) - successful,
        artifact=None,
        outcomes=tuple(outcomes),
        metadata={"dataset": DATASET, "marker": dict(marker or {}), "reason": reason},
    )
    report = {
        "success": False,
        "reason": reason,
        "run_id": run_id,
        "expected_champions": len(rankings),
        "successful_champions": successful,
        "failed_champions": len(outcomes) - successful,
        "downloaded_bytes": bytes_used,
        "check_status": "unknown",
        "upstream_revision": str((marker or {}).get("dataPath") or ""),
        "upstream_marker": dict(marker or {}),
        "applied_revision": None,
        **failure_fields(reason, responses, failure_fingerprint=failure_fingerprint),
    }
    write_run_diagnostics(manifest, report=report)
    return report


def refresh_aramkit(
    *,
    force: bool = False,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
    fetcher: Fetcher | None = None,
    catalog_binding: CatalogBinding | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    stop_event: threading.Event | None = None,
    now: datetime | None = None,
    raw_cache_root: Path | None = None,
    conditional_cache_root: Path | None = None,
    previous_failure_fingerprint: str = "",
    context: Callable[[], DownloadContext] | None = None,
    on_rankings: Callable[[Mapping[str, Any], list[dict[str, Any]]], None] | None = None,
    on_detail: Callable[[Mapping[str, Any], str, Mapping[str, Any]], None] | None = None,
    incremental_only: bool = False,
) -> dict[str, Any]:
    """抓取完整同版本 ARAMKit 候选，并只返回/写出 candidate pointer。"""

    if promote_current:
        raise SourceRunValidationError("正式 source current 只能由 cohort promotion 切换")
    if incremental_only and (on_rankings is None or on_detail is None):
        raise ValueError("incremental download requires ranking and champion unit consumers")
    if concurrency <= 0 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"ARAMKit concurrency 必须在 1..{MAX_CONCURRENCY}")
    fetch = with_conditional_versions(
        fetcher or _default_fetcher,
        conditional_cache_root or raw_cache_root,
        versions_url=VERSIONS_URL,
    )
    binding = catalog_binding or CatalogBinding.active()
    budget = _ByteBudget(per_response=MAX_RESPONSE_BYTES, total=MAX_TOTAL_BYTES)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}-all"
    started_at = utc_now_iso()
    rankings: list[dict[str, Any]] = []
    results: dict[str, _DetailResult] = {}
    marker: dict[str, Any] = {}
    validation_fingerprint = ""
    try:
        version = _version_payload(fetch, budget)
        marker = version_marker(version)
        if not force:
            current, reusable = load_reusable_current(
                marker,
                now=now or datetime.now(timezone.utc),
                validator=validate_scoped_stats_artifact,
                catalog_generation_id=binding.generation_id,
                catalog_sha256=binding.content_sha256,
            )
            if reusable:
                return {
                    "success": True,
                    "reason": "not_stale",
                    "pointer": current,
                    "marker": marker,
                    "check_status": "up_to_date",
                    "upstream_revision": str(marker.get("dataPath") or ""),
                    "applied_revision": str(marker.get("dataPath") or ""),
                    "upstream_marker": marker,
                    "retry_after_seconds": None,
                }
        validation_fingerprint = validation_input_fingerprint(
            marker,
            catalog_generation_id=binding.generation_id,
            catalog_sha256=binding.content_sha256,
        )
        if previous_failure_fingerprint == validation_fingerprint:
            return duplicate_validation_result(marker, validation_fingerprint)
        if raw_cache_root is not None:
            cache = RawResponseCache(raw_cache_root, source="aramkit",
                                     revision=json.dumps(marker, sort_keys=True))
            network_fetch = fetch

            def cached_fetch(url: str, **kwargs: object) -> object:
                # versions is mutable discovery; only fixed dataPath resources are immutable.
                if url == VERSIONS_URL:
                    return network_fetch(url, **kwargs)
                try:
                    body = cache.get(url)
                except RawResponseIntegrityError:
                    body = None
                if body is not None:
                    return _Response(
                        url,
                        body,
                        200,
                        "",
                        "",
                        0,
                        1,
                        utc_now_iso(),
                        from_cache=True,
                    )
                response = _coerce_response(url, network_fetch(url, **kwargs))
                if response.failure_kind is None and len(response.body) <= MAX_RESPONSE_BYTES:
                    try:
                        cache.put(url, response.body)
                    except RawResponseIntegrityError:
                        pass
                return response

            fetch = cached_fetch
        rankings_url = f"{DATA_BASE_URL}/{version['dataPath']}/stats/{DATASET}/champion-rankings.json"
        rankings = normalize_rankings(
            _require_json_response(fetch, rankings_url, budget, context="champion-rankings")
        )
        ranking_ids = {str(item["id"]) for item in rankings}
        unknown_champions = sorted(ranking_ids - binding.champion_ids, key=int)
        if unknown_champions:
            raise AramkitRefreshError(
                "catalog_binding_failed",
                f"ARAMKit 包含 Catalog 未知英雄：{unknown_champions[:20]}",
            )
        if on_rankings is not None:
            on_rankings(version, rankings)

        def publish_detail(champion_id: str, result: _DetailResult) -> None:
            if not result.success or result.normalized is None or on_detail is None:
                return
            unknown = _all_augment_ids(result.normalized) - binding.augment_ids
            if unknown - binding.compatible_extra_augment_ids:
                return  # full result retains failure; never publish unbound partial data.
            normalized_detail = _filter_detail_to_catalog(result.normalized, binding.augment_ids)
            if not normalized_detail["augments"]["all"] or any(
                not rows for rows in normalized_detail["augments"]["stages"].values()
            ):
                return
            on_detail(version, champion_id, normalized_detail)
        stop_event = stop_event or threading.Event()
        if stop_event.is_set():
            raise AramkitRefreshError("cancelled", "ARAMKit worker 已取消")
        initial = _detail_pass(
            fetch,
            budget,
            version,
            rankings,
            concurrency=concurrency,
            stop_event=stop_event,
            context=context,
            on_detail=publish_detail,
        )
        results.update((item.champion_id, item) for item in initial)
        retry_rankings = [
            ranking
            for ranking in rankings
            if (item := results.get(str(ranking["id"]))) is not None and item.retryable
        ]
        if retry_rankings and not stop_event.is_set():
            retried = _detail_pass(
                fetch,
                budget,
                version,
                retry_rankings,
                concurrency=min(RETRY_CONCURRENCY, len(retry_rankings)),
                stop_event=stop_event,
                context=context,
                on_detail=publish_detail,
            )
            results.update((item.champion_id, item) for item in retried)
        failures = {
            champion_id: results.get(champion_id)
            for champion_id in ranking_ids
            if not (results.get(champion_id) and results[champion_id].success)
        }
        if failures:
            failure_reasons = {item.reason for item in failures.values() if item is not None}
            reason = next(
                (
                    candidate
                    for candidate in ("response_too_large", "download_budget_exceeded")
                    if candidate in failure_reasons
                ),
                "",
            )
            if not reason and any(item and item.response and item.response.blocking for item in failures.values()):
                reason = "blocked"
            if not reason and any(value.startswith("schema_changed") for value in failure_reasons):
                reason = "schema_changed"
            reason = reason or "detail_failed"
            return _write_failure(
                run_id=run_id,
                binding=binding,
                started_at=started_at,
                rankings=rankings,
                results=results,
                reason=reason,
                marker=marker,
                bytes_used=budget.used,
                failure_fingerprint=(validation_fingerprint if reason == "schema_changed" else ""),
            )
        unknown_augments = {
            augment_id
            for item in results.values()
            for augment_id in _all_augment_ids(item.normalized or {})
            if augment_id not in binding.augment_ids
        }
        incompatible_unknown_augments = sorted(
            unknown_augments - binding.compatible_extra_augment_ids
        )
        if incompatible_unknown_augments:
            raise AramkitRefreshError(
                "catalog_binding_failed",
                f"ARAMKit 包含 Catalog 未知海克斯：{incompatible_unknown_augments[:20]}",
            )
        compatibility_filtered_augment_ids = sorted(
            unknown_augments & binding.compatible_extra_augment_ids
        )
        final_marker = version_marker(_version_payload(fetch, budget))
        if final_marker != marker:
            raise AramkitRefreshError("marker_drift", "ARAMKit 抓取期间版本 marker 发生变化")
        if incremental_only:
            # The worker has already emitted independently validated immutable units.
            # Do not duplicate every champion into a fresh full-run tree on each recheck.
            return {
                "success": True,
                "reason": "units_complete",
                "marker": marker,
                "expected_champions": len(rankings),
                "successful_champions": len(results),
                "check_status": "changed",
                "upstream_revision": str(marker.get("dataPath") or ""),
                "applied_revision": str(marker.get("dataPath") or ""),
                "upstream_marker": marker,
                "retry_after_seconds": None,
            }
        normalized = {
            champion_id: _filter_detail_to_catalog(item.normalized, binding.augment_ids)
            for champion_id, item in results.items()
            if item.normalized
        }
        index_path, record_count = _write_artifact(run_id, version, normalized)
        artifact = build_artifact_descriptor(
            index_path,
            role="scoped_stats",
            relative_path="scoped_stats/manifest.json",
            record_count=record_count,
            content_schema_version=1,
        )
        outcomes = tuple(
            ItemOutcome(
                item_id=champion_id,
                state="success",
                stage="detail",
                record_count=len(normalized[champion_id]["augments"]["all"]),
                attempt=results[champion_id].response.attempt() if results[champion_id].response else None,
            )
            for champion_id in sorted(normalized, key=int)
        )
        manifest = SourceRunManifestV2(
            source="aramkit",
            run_id=run_id,
            catalog_generation_id=binding.generation_id,
            catalog_sha256=binding.content_sha256,
            health=SourceHealth.HEALTHY,
            started_at=started_at,
            completed_at=utc_now_iso(),
            expected_items=len(rankings),
            successful_items=len(outcomes),
            confirmed_empty_items=0,
            failed_items=0,
            artifact=artifact,
            outcomes=outcomes,
            metadata={
                "dataset": DATASET,
                "version": str(version["version"]),
                "data_path": str(version["dataPath"]),
                "marker": marker,
                "downloaded_bytes": budget.used,
                "compatibility_filtered_augment_ids": compatibility_filtered_augment_ids,
            },
        )
        report = {
            "success": True,
            "reason": "ready",
            "run_id": run_id,
            "expected_champions": len(rankings),
            "successful_champions": len(outcomes),
            "record_count": record_count,
            "downloaded_bytes": budget.used,
            "marker": marker,
            "compatibility_filtered_augment_ids": compatibility_filtered_augment_ids,
        }
        pointer = publish_source_run(
            manifest,
            report=report,
            promote_current=False,
            pointer_output=pointer_output,
        )
        validate_scoped_stats_artifact(pointer)
        return {
            **report,
            "pointer": pointer,
            "check_status": "changed",
            "upstream_revision": str(marker.get("dataPath") or ""),
            "applied_revision": str(marker.get("dataPath") or ""),
            "upstream_marker": marker,
            "retry_after_seconds": None,
        }
    except (AramkitRefreshError, SchemaValidationError, SourceRunValidationError, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, AramkitRefreshError) else (
            "schema_changed" if isinstance(exc, SchemaValidationError) else "publish_failed"
        )
        validation_failure = reason in {"schema_changed", "catalog_binding_failed", "marker_drift"}
        response = (exc.response if isinstance(exc, AramkitRefreshError)
                    and isinstance(exc.response, _Response) else None)
        if validation_failure and not validation_fingerprint:
            validation_fingerprint = validation_input_fingerprint(
                marker,
                catalog_generation_id=binding.generation_id,
                catalog_sha256=binding.content_sha256,
                response_body=response.body if response is not None else b"",
            )
        if validation_failure and previous_failure_fingerprint == validation_fingerprint:
            return duplicate_validation_result(marker, validation_fingerprint)
        return _write_failure(
            run_id=run_id,
            binding=binding,
            started_at=started_at,
            rankings=rankings,
            results=results,
            reason=reason,
            marker=marker,
            bytes_used=budget.used,
            response=response,
            failure_fingerprint=validation_fingerprint if validation_failure else "",
        )


__all__ = [
    "AramkitRefreshError",
    "CatalogBinding",
    "DATA_BASE_URL",
    "DEFAULT_CONCURRENCY",
    "MAX_CONCURRENCY",
    "MAX_RESPONSE_BYTES",
    "MAX_TOTAL_BYTES",
    "RETRY_CONCURRENCY",
    "probe_aramkit_upstream_marker",
    "refresh_aramkit",
    "validate_scoped_stats_artifact",
]
