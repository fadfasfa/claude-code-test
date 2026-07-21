"""ARAMKit 静态 JSON 全量抓取、校验、留证与两次运行比较。

本模块只用于开发期验证，不发布 DataService 指针，也不读取名称、描述、图标等
``resourcePath`` 资源。源站详情原文与最小统计投影同时留存，便于未来决定是否
替换 ARAMGG 时复核 schema 和数据覆盖。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import statistics
import tempfile
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests


DATA_BASE_URL = "https://data.aramkit.com"
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 8
DEFAULT_TIMEOUT_SECONDS = 20.0
RETRY_CONCURRENCY = 2
SCHEMA_VERSION = 1
USER_AGENT = "HextechNexus-ARAMKit-Probe/1.0"
RUN_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = RUN_DIR / "var" / "aramkit_probe"
DATASETS = frozenset({"all", "high"})
STAGES = ("1", "2", "3", "4")
RATE_FIELDS = ("winRate", "pickRate", "blueWinRate", "redWinRate")
CHAMPION_STAT_FIELDS = ("sampleCount", *RATE_FIELDS)
AUGMENT_FIELDS = ("id", "rank", "sampleCount", *RATE_FIELDS)


class ProbeError(RuntimeError):
    """可诊断的抓取或完整性失败。"""


class SchemaValidationError(ProbeError):
    """ARAMKit 响应不再符合探针所需 schema。"""


@dataclass(frozen=True)
class FetchConfig:
    dataset: str = "all"
    concurrency: int = DEFAULT_CONCURRENCY
    version: str = "latest"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    output_root: Path = DEFAULT_OUTPUT_ROOT

    def __post_init__(self) -> None:
        if self.dataset not in DATASETS:
            raise ValueError(f"dataset 只允许 all/high：{self.dataset}")
        if not 1 <= int(self.concurrency) <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency 必须位于 1..{MAX_CONCURRENCY}")
        if not str(self.version).strip():
            raise ValueError("version 不能为空")
        if float(self.timeout_seconds) <= 0:
            raise ValueError("timeout_seconds 必须大于 0")


@dataclass(frozen=True)
class TransportResponse:
    status_code: int | None
    body: bytes
    elapsed_ms: float
    error_kind: str = ""
    error: str = ""

    @property
    def blocking(self) -> bool:
        return self.status_code in {403, 429}

    @property
    def retryable(self) -> bool:
        return self.status_code is None or bool(self.status_code and self.status_code >= 500)


Transport = Callable[[str, float], TransportResponse]


@dataclass
class DetailOutcome:
    champion_id: str
    url: str
    response: TransportResponse
    raw_body: bytes = b""
    normalized: dict[str, Any] | None = None
    error: str = ""
    skipped: bool = False

    @property
    def success(self) -> bool:
        return self.response.status_code == 200 and self.normalized is not None and not self.error

    @property
    def retryable(self) -> bool:
        return not self.skipped and self.response.retryable


@dataclass(frozen=True)
class ProbeResult:
    complete: bool
    run_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]


class RequestsTransport:
    """每个 worker 复用自己的 Session，避免跨线程共享可变连接状态。"""

    def __init__(self, *, user_agent: str = USER_AGENT) -> None:
        self._user_agent = user_agent
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self._user_agent, "Accept": "application/json"})
            self._local.session = session
        return session

    def __call__(self, url: str, timeout_seconds: float) -> TransportResponse:
        started = time.monotonic()
        try:
            response = self._session().get(url, timeout=timeout_seconds)
            body = response.content
            return TransportResponse(
                status_code=int(response.status_code),
                body=body,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error="" if response.status_code == 200 else f"http_{response.status_code}",
            )
        except requests.Timeout as exc:
            return TransportResponse(
                status_code=None,
                body=b"",
                elapsed_ms=(time.monotonic() - started) * 1000,
                error_kind="timeout",
                error=str(exc),
            )
        except requests.ConnectionError as exc:
            return TransportResponse(
                status_code=None,
                body=b"",
                elapsed_ms=(time.monotonic() - started) * 1000,
                error_kind="connection_error",
                error=str(exc),
            )
        except requests.RequestException as exc:
            return TransportResponse(
                status_code=None,
                body=b"",
                elapsed_ms=(time.monotonic() - started) * 1000,
                error_kind="request_error",
                error=str(exc),
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(dataset: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}-{dataset}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _write_gzip_json(path: Path, payload: Any, *, canonical: bool = False) -> dict[str, Any]:
    raw = _canonical_json_bytes(payload) if canonical else _pretty_json_bytes(payload)
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    _atomic_write_bytes(path, compressed)
    return {"path": path.name, "size": len(compressed), "sha256": _sha256(compressed)}


def _write_gzip_raw(path: Path, raw: bytes) -> dict[str, Any]:
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    _atomic_write_bytes(path, compressed)
    return {"size": len(compressed), "sha256": _sha256(compressed)}


def _decode_object(body: bytes, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"{context} 不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"{context} 顶层必须是对象")
    return payload


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{context} 必须是对象")
    return value


def _require_list(value: Any, *, context: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{context} 必须是数组")
    if nonempty and not value:
        raise SchemaValidationError(f"{context} 不能为空")
    return value


def _positive_int(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是正整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是正整数") from exc
    if normalized <= 0 or float(value) != float(normalized):
        raise SchemaValidationError(f"{context} 必须是正整数")
    return normalized


def _nonnegative_int(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是非负整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是非负整数") from exc
    if normalized < 0 or float(value) != float(normalized):
        raise SchemaValidationError(f"{context} 必须是非负整数")
    return normalized


def _rate(value: Any, *, context: str) -> float:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数") from exc
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数")
    return normalized


def _champion_summary(payload: Mapping[str, Any], *, context: str, nested_stats: bool) -> dict[str, Any]:
    champion_id = str(payload.get("id", "")).strip()
    if not champion_id or not champion_id.isdigit():
        raise SchemaValidationError(f"{context}.id 必须是数字英雄 ID")
    stats = _require_mapping(payload.get("stats"), context=f"{context}.stats") if nested_stats else payload
    tier = str(payload.get("tier", "")).strip()
    if not tier:
        raise SchemaValidationError(f"{context}.tier 不能为空")
    result: dict[str, Any] = {
        "id": champion_id,
        "rank": _positive_int(payload.get("rank"), context=f"{context}.rank"),
        "tier": tier,
        "stats": {
            "sampleCount": _nonnegative_int(stats.get("sampleCount"), context=f"{context}.sampleCount")
        },
    }
    for field in RATE_FIELDS:
        result["stats"][field] = _rate(stats.get(field), context=f"{context}.{field}")
    return result


def _normalize_augment(raw: Any, *, context: str, is_all: bool) -> dict[str, Any]:
    item = _require_mapping(raw, context=context)
    result: dict[str, Any] = {
        "id": _positive_int(item.get("id"), context=f"{context}.id"),
        "rank": _positive_int(item.get("rank"), context=f"{context}.rank"),
        "sampleCount": _nonnegative_int(item.get("sampleCount"), context=f"{context}.sampleCount"),
    }
    for field in RATE_FIELDS:
        result[field] = _rate(item.get(field), context=f"{context}.{field}")
    if is_all:
        stage_agnostic = item.get("stageAgnostic")
        if not isinstance(stage_agnostic, bool):
            raise SchemaValidationError(f"{context}.stageAgnostic 必须是布尔值")
        available = _require_list(item.get("availableStages"), context=f"{context}.availableStages")
        normalized_stages = [str(value) for value in available]
        if any(value not in STAGES for value in normalized_stages) or len(set(normalized_stages)) != len(normalized_stages):
            raise SchemaValidationError(f"{context}.availableStages 非法")
        result["stageAgnostic"] = stage_agnostic
        result["availableStages"] = normalized_stages
    return result


def _normalize_augment_scope(raw_rows: Any, *, context: str, is_all: bool) -> list[dict[str, Any]]:
    rows = _require_list(raw_rows, context=context, nonempty=True)
    normalized = [_normalize_augment(row, context=f"{context}[{index}]", is_all=is_all) for index, row in enumerate(rows)]
    ids = [row["id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise SchemaValidationError(f"{context} 存在重复海克斯 ID")
    return normalized


def normalize_detail(payload: Mapping[str, Any], ranking: Mapping[str, Any]) -> dict[str, Any]:
    expected_id = str(ranking["id"])
    champion = _require_mapping(payload.get("champion"), context=f"champion[{expected_id}]")
    normalized_champion = _champion_summary(champion, context=f"champion[{expected_id}]", nested_stats=True)
    if normalized_champion != dict(ranking):
        raise SchemaValidationError(f"champion[{expected_id}] 与排行概要不一致，疑似版本混用")

    augments = _require_mapping(payload.get("augments"), context=f"champion[{expected_id}].augments")
    stages = _require_mapping(augments.get("stages"), context=f"champion[{expected_id}].augments.stages")
    normalized_stages = {
        stage: _normalize_augment_scope(
            stages.get(stage),
            context=f"champion[{expected_id}].augments.stages.{stage}",
            is_all=False,
        )
        for stage in STAGES
    }
    normalized_champion["augments"] = {
        "all": _normalize_augment_scope(
            augments.get("all"), context=f"champion[{expected_id}].augments.all", is_all=True
        ),
        "stages": normalized_stages,
    }
    return normalized_champion


def _request_json_with_retry(
    transport: Transport,
    url: str,
    *,
    timeout_seconds: float,
    context: str,
) -> tuple[dict[str, Any], list[TransportResponse]]:
    attempts: list[TransportResponse] = []
    for attempt in range(2):
        response = transport(url, timeout_seconds)
        attempts.append(response)
        if response.status_code == 200:
            return _decode_object(response.body, context=context), attempts
        if response.blocking or not response.retryable or attempt == 1:
            break
        time.sleep(0.35)
    last = attempts[-1]
    raise ProbeError(f"{context} 请求失败：status={last.status_code} kind={last.error_kind} error={last.error}")


def _resolve_version(payload: Mapping[str, Any], requested: str) -> dict[str, Any]:
    versions = _require_list(payload.get("versions"), context="versions", nonempty=True)
    latest = str(payload.get("latest", "")).strip()
    target = latest if requested == "latest" else requested
    for raw in versions:
        row = _require_mapping(raw, context="versions[]")
        if str(row.get("version", "")).strip() != target:
            continue
        data_path = str(row.get("dataPath", "")).strip().strip("/")
        if not data_path:
            raise SchemaValidationError(f"版本 {target} 缺少 dataPath")
        return {
            "version": target,
            "dataPath": data_path,
            "allMatches": _nonnegative_int(row.get("allMatches"), context=f"version[{target}].allMatches"),
            "highMatches": _nonnegative_int(row.get("highMatches"), context=f"version[{target}].highMatches"),
            "dataStartTimeUnixMs": _nonnegative_int(
                row.get("dataStartTimeUnixMs"), context=f"version[{target}].dataStartTimeUnixMs"
            ),
            "dataEndTimeUnixMs": _nonnegative_int(
                row.get("dataEndTimeUnixMs"), context=f"version[{target}].dataEndTimeUnixMs"
            ),
            "buildTimeUnixMs": _nonnegative_int(
                row.get("buildTimeUnixMs", 0), context=f"version[{target}].buildTimeUnixMs"
            ),
        }
    raise SchemaValidationError(f"未找到公开版本：{target}")


def _normalize_rankings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _require_list(payload.get("rows"), context="champion-rankings.rows", nonempty=True)
    normalized = [
        _champion_summary(_require_mapping(row, context=f"champion-rankings.rows[{index}]"), context=f"ranking[{index}]", nested_stats=False)
        for index, row in enumerate(rows)
    ]
    ids = [row["id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise SchemaValidationError("champion-rankings 存在重复英雄 ID")
    return normalized


def _detail_url(version: Mapping[str, Any], dataset: str, champion_id: str) -> str:
    return f"{DATA_BASE_URL}/{version['dataPath']}/stats/{dataset}/champion-details/{champion_id}.json"


def _fetch_detail(
    transport: Transport,
    version: Mapping[str, Any],
    dataset: str,
    ranking: Mapping[str, Any],
    timeout_seconds: float,
    stop_event: threading.Event,
) -> DetailOutcome:
    champion_id = str(ranking["id"])
    url = _detail_url(version, dataset, champion_id)
    if stop_event.is_set():
        return DetailOutcome(
            champion_id=champion_id,
            url=url,
            response=TransportResponse(None, b"", 0, error_kind="cancelled", error="circuit_open"),
            error="circuit_open",
            skipped=True,
        )
    response = transport(url, timeout_seconds)
    if response.blocking:
        stop_event.set()
    if response.status_code != 200:
        return DetailOutcome(champion_id, url, response, error=response.error or f"http_{response.status_code}")
    try:
        payload = _decode_object(response.body, context=f"champion-detail[{champion_id}]")
        normalized = normalize_detail(payload, ranking)
    except ProbeError as exc:
        return DetailOutcome(champion_id, url, response, raw_body=response.body, error=str(exc))
    return DetailOutcome(champion_id, url, response, raw_body=response.body, normalized=normalized)


def _run_detail_pass(
    transport: Transport,
    version: Mapping[str, Any],
    config: FetchConfig,
    rankings: list[dict[str, Any]],
    *,
    concurrency: int,
    stop_event: threading.Event,
) -> list[DetailOutcome]:
    outcomes: list[DetailOutcome] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="aramkit-probe") as executor:
        future_map: dict[Future[DetailOutcome], Mapping[str, Any]] = {
            executor.submit(
                _fetch_detail,
                transport,
                version,
                config.dataset,
                ranking,
                config.timeout_seconds,
                stop_event,
            ): ranking
            for ranking in rankings
        }
        for future in as_completed(future_map):
            ranking = future_map[future]
            try:
                outcome = future.result()
            except Exception as exc:  # pragma: no cover - worker 最后防线
                champion_id = str(ranking["id"])
                outcome = DetailOutcome(
                    champion_id,
                    _detail_url(version, config.dataset, champion_id),
                    TransportResponse(None, b"", 0, error_kind="worker_error", error=str(exc)),
                    error=f"worker_error:{exc}",
                )
            outcomes.append(outcome)
            if outcome.response.blocking:
                for pending in future_map:
                    pending.cancel()
    return outcomes


def _latency_summary(responses: list[TransportResponse]) -> dict[str, Any]:
    values = sorted(float(item.elapsed_ms) for item in responses)
    if not values:
        return {"p50Ms": 0.0, "p95Ms": 0.0, "maxMs": 0.0}
    p95_index = min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)
    return {
        "p50Ms": round(float(statistics.median(values)), 3),
        "p95Ms": round(values[p95_index], 3),
        "maxMs": round(values[-1], 3),
    }


def _manifest_base(config: FetchConfig, run_id: str, started_at: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": "aramkit",
        "runId": run_id,
        "dataset": config.dataset,
        "complete": False,
        "startedAt": started_at,
        "completedAt": "",
        "durationSeconds": 0.0,
        "config": {
            "version": config.version,
            "concurrency": config.concurrency,
            "retryConcurrency": RETRY_CONCURRENCY,
            "timeoutSeconds": config.timeout_seconds,
        },
        "version": {},
        "requests": {},
        "coverage": {},
        "artifacts": {},
        "errors": [],
    }


def run_fetch(config: FetchConfig, *, transport: Transport | None = None) -> ProbeResult:
    """执行一次独立全量抓取；失败也会留下 manifest 作为诊断证据。"""

    transport = transport or RequestsTransport()
    run_id = _run_id(config.dataset)
    run_dir = Path(config.output_root) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    manifest = _manifest_base(config, run_id, started_at)
    manifest_path = run_dir / "manifest.json"
    request_responses: list[TransportResponse] = []
    outcomes_by_id: dict[str, DetailOutcome] = {}
    rankings: list[dict[str, Any]] = []

    try:
        versions_url = f"{DATA_BASE_URL}/data/versions.json"
        versions_payload, version_attempts = _request_json_with_retry(
            transport,
            versions_url,
            timeout_seconds=config.timeout_seconds,
            context="data/versions.json",
        )
        request_responses.extend(version_attempts)
        version = _resolve_version(versions_payload, config.version)
        manifest["version"] = version

        rankings_url = f"{DATA_BASE_URL}/{version['dataPath']}/stats/{config.dataset}/champion-rankings.json"
        rankings_payload, ranking_attempts = _request_json_with_retry(
            transport,
            rankings_url,
            timeout_seconds=config.timeout_seconds,
            context="champion-rankings.json",
        )
        request_responses.extend(ranking_attempts)
        rankings = _normalize_rankings(rankings_payload)

        stop_event = threading.Event()
        initial = _run_detail_pass(
            transport,
            version,
            config,
            rankings,
            concurrency=config.concurrency,
            stop_event=stop_event,
        )
        request_responses.extend(item.response for item in initial if not item.skipped)
        outcomes_by_id.update({item.champion_id: item for item in initial})

        retry_rankings = [
            ranking
            for ranking in rankings
            if (outcome := outcomes_by_id.get(str(ranking["id"]))) is not None and outcome.retryable
        ]
        if retry_rankings and not stop_event.is_set():
            time.sleep(0.35)
            retried = _run_detail_pass(
                transport,
                version,
                config,
                retry_rankings,
                concurrency=min(RETRY_CONCURRENCY, len(retry_rankings)),
                stop_event=stop_event,
            )
            request_responses.extend(item.response for item in retried if not item.skipped)
            outcomes_by_id.update({item.champion_id: item for item in retried})

        expected_ids = {str(row["id"]) for row in rankings}
        successful = {champion_id: item for champion_id, item in outcomes_by_id.items() if item.success}
        errors = []
        for champion_id in sorted(expected_ids - set(successful), key=int):
            outcome = outcomes_by_id.get(champion_id)
            errors.append(
                {
                    "championId": champion_id,
                    "status": outcome.response.status_code if outcome else None,
                    "kind": outcome.response.error_kind if outcome else "missing_outcome",
                    "error": outcome.error if outcome else "missing_outcome",
                    "url": outcome.url if outcome else _detail_url(version, config.dataset, champion_id),
                }
            )
        manifest["errors"] = errors

        raw_files = []
        for champion_id, outcome in sorted(successful.items(), key=lambda entry: int(entry[0])):
            relative_path = Path("raw") / "champion-details" / f"{champion_id}.json.gz"
            descriptor = _write_gzip_raw(run_dir / relative_path, outcome.raw_body)
            raw_files.append({"championId": champion_id, "path": relative_path.as_posix(), **descriptor})

        snapshot = {
            "schemaVersion": SCHEMA_VERSION,
            "source": "aramkit",
            "dataset": config.dataset,
            "version": version["version"],
            "dataPath": version["dataPath"],
            "sourceMatches": version[f"{config.dataset}Matches"],
            "champions": {
                champion_id: successful[champion_id].normalized
                for champion_id in sorted(successful, key=int)
            },
        }
        snapshot_path = run_dir / "snapshot.json.gz"
        snapshot_descriptor = _write_gzip_json(snapshot_path, snapshot, canonical=True)
        snapshot_descriptor["path"] = "snapshot.json.gz"

        stage_counts = {
            stage: sum(len(item.normalized["augments"]["stages"][stage]) for item in successful.values())
            for stage in STAGES
        }
        manifest["coverage"] = {
            "expectedChampions": len(expected_ids),
            "successfulChampions": len(successful),
            "missingChampionIds": sorted(expected_ids - set(successful), key=int),
            "unexpectedChampionIds": sorted(set(successful) - expected_ids, key=int),
            "augmentAllRecords": sum(len(item.normalized["augments"]["all"]) for item in successful.values()),
            "augmentStageRecords": stage_counts,
        }
        manifest["artifacts"] = {
            "snapshot": snapshot_descriptor,
            "raw": {
                "count": len(raw_files),
                "totalSize": sum(item["size"] for item in raw_files),
                "files": raw_files,
            },
            "contentFingerprint": _sha256(_canonical_json_bytes(snapshot)),
        }
        manifest["complete"] = not errors and set(successful) == expected_ids
    except (ProbeError, OSError, ValueError) as exc:
        manifest["errors"] = [{"kind": exc.__class__.__name__, "error": str(exc)}]

    manifest["completedAt"] = _utc_now()
    manifest["durationSeconds"] = round(time.monotonic() - started_monotonic, 3)
    status_counts = Counter(str(item.status_code) if item.status_code is not None else item.error_kind or "network" for item in request_responses)
    manifest["requests"] = {
        "attempts": len(request_responses),
        "statusCounts": dict(sorted(status_counts.items())),
        "downloadedBytes": sum(len(item.body) for item in request_responses),
        **_latency_summary(request_responses),
    }
    _atomic_write_bytes(manifest_path, _pretty_json_bytes(manifest))
    return ProbeResult(bool(manifest["complete"]), run_dir, manifest_path, manifest)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProbeError(f"无法读取 manifest：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeError(f"manifest 顶层不是对象：{path}")
    return payload


def _load_snapshot(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    snapshot_info = _require_mapping(
        _require_mapping(manifest.get("artifacts"), context="manifest.artifacts").get("snapshot"),
        context="manifest.artifacts.snapshot",
    )
    relative = str(snapshot_info.get("path", ""))
    target = (run_dir / relative).resolve()
    if run_dir.resolve() not in target.parents:
        raise ProbeError(f"snapshot 路径越界：{target}")
    data = target.read_bytes()
    if _sha256(data) != str(snapshot_info.get("sha256", "")) or len(data) != int(snapshot_info.get("size", -1)):
        raise ProbeError(f"snapshot 哈希或大小不一致：{target}")
    try:
        payload = json.loads(gzip.decompress(data).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProbeError(f"snapshot 无法解压解析：{target}: {exc}") from exc
    return _decode_object(_canonical_json_bytes(payload), context=str(target))


def compare_latest_runs(
    *,
    dataset: str = "all",
    latest: int = 2,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    if dataset not in DATASETS:
        raise ValueError(f"dataset 只允许 all/high：{dataset}")
    if latest != 2:
        raise ValueError("当前比较器固定比较最近两次完整 run，--latest 必须为 2")
    runs_root = Path(output_root) / "runs"
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in runs_root.glob("*/manifest.json"):
        manifest = _load_manifest(manifest_path)
        if manifest.get("dataset") == dataset and manifest.get("complete") is True:
            candidates.append((manifest_path.parent, manifest))
    candidates.sort(key=lambda item: str(item[1].get("startedAt", "")))
    if len(candidates) < latest:
        raise ProbeError(f"{dataset} 完整 run 不足 {latest} 次")
    (run_a, manifest_a), (run_b, manifest_b) = candidates[-2:]
    snapshot_a = _load_snapshot(run_a, manifest_a)
    snapshot_b = _load_snapshot(run_b, manifest_b)
    champions_a = _require_mapping(snapshot_a.get("champions"), context="snapshot_a.champions")
    champions_b = _require_mapping(snapshot_b.get("champions"), context="snapshot_b.champions")
    ids = sorted(set(champions_a) | set(champions_b), key=int)
    changed = [champion_id for champion_id in ids if champions_a.get(champion_id) != champions_b.get(champion_id)]
    data_path_a = str(snapshot_a.get("dataPath", ""))
    data_path_b = str(snapshot_b.get("dataPath", ""))
    same_data_path = data_path_a == data_path_b
    fingerprint_a = str(_require_mapping(manifest_a.get("artifacts"), context="manifest_a.artifacts").get("contentFingerprint", ""))
    fingerprint_b = str(_require_mapping(manifest_b.get("artifacts"), context="manifest_b.artifacts").get("contentFingerprint", ""))
    passed = not same_data_path or (fingerprint_a == fingerprint_b and not changed)
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "dataset": dataset,
        "passed": passed,
        "sameDataPath": same_data_path,
        "runA": str(run_a),
        "runB": str(run_b),
        "versionA": snapshot_a.get("version"),
        "versionB": snapshot_b.get("version"),
        "dataPathA": data_path_a,
        "dataPathB": data_path_b,
        "fingerprintA": fingerprint_a,
        "fingerprintB": fingerprint_b,
        "championCountA": len(champions_a),
        "championCountB": len(champions_b),
        "changedChampionCount": len(changed),
        "changedChampionIds": changed,
        "createdAt": _utc_now(),
    }
    comparisons_root = Path(output_root) / "comparisons"
    report_path = comparisons_root / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{dataset}.json"
    _atomic_write_bytes(report_path, _pretty_json_bytes(report))
    report["reportPath"] = str(report_path)
    return report
