"""Blitz ARAM Mayhem 排名的静态抓取、Catalog 绑定与候选发布。

生产路径只访问公开 JSON 端点，不抓 Blitz 页面，不使用 browser、登录态、代理
或反爬绕过。来源只承诺 tier 排名，不生成或推断胜率、选择率与样本量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from hextech.contracts import FetchAttempt, ItemOutcome, SourceHealth, SourcePointerV2, SourceRunManifestV2, utc_now_iso
from hextech.contracts.models import FailureKind
from hextech.infrastructure.transport.scrapling_client import fetch_text
from hextech.infrastructure.transport.conditional_response import parse_retry_after_seconds
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data
from hextech.modules.data.catalog.versioned import (
    load_active_catalog,
    load_runtime_catalog_from_pointer,
    sha256_file,
)
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import (
    SourceRunValidationError,
    build_artifact_descriptor,
    load_source_current,
    load_source_run_manifest,
    publish_source_run,
    source_run_artifact_path,
    source_run_dir,
    write_run_diagnostics,
)

from .schema import (
    BlitzSchemaError,
    decode_object,
    normalize_payload,
    project_normalized_rows,
    validate_artifact,
)
from .refresh_cache import (
    PARSER_REVISION,
    content_sha256,
    failure_identity,
    with_conditional_response,
)


DATA_URL = "https://data.v2.iesdev.com/api/v1/query_objects/prod/lol/aram_mayhem_augments"
TIMEOUT_MS = 15_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_PRODUCTION_COVERAGE = 0.95
MIN_ACTIVE_PRODUCTION_COVERAGE = 0.85
MIN_ACTIVE_PREVIOUS_RECORD_RATIO = 0.90
CoveragePolicy = Literal["catalog_adoption", "active_partial"]
Fetcher = Callable[..., object]


class BlitzRefreshError(RuntimeError):
    """Blitz 候选无法满足完整性或 Catalog 绑定合同。"""

    def __init__(
        self,
        reason: str,
        message: str = "",
        *,
        response: object | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.response = response
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class CatalogBinding:
    generation_id: str
    content_sha256: str
    augment_ids: frozenset[str]
    champion_ids: frozenset[str]
    production_ids: frozenset[str]
    compatible_extra_augment_ids: frozenset[str] = frozenset()

    @classmethod
    def active(
        cls,
        *,
        compatibility_pointer: str | Path | None = None,
    ) -> "CatalogBinding":
        catalog = load_active_catalog()
        augment_ids = {
            str(int(item["cdragon_id"]))
            for item in load_augment_manifest_entries(catalog.root)
            if str(item.get("cdragon_id") or "").lstrip("-").isdigit() and int(item["cdragon_id"]) > 0
        }
        champion_ids = set(load_champion_core_data(catalog.root))
        try:
            assets = json.loads((catalog.root / "augment_assets.v1.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlitzRefreshError("catalog_assets_unavailable") from exc
        entries = assets.get("entries") if isinstance(assets, Mapping) else None
        if not isinstance(entries, list):
            raise BlitzRefreshError("catalog_assets_unavailable")
        production_ids = {
            str(item.get("canonical_id") or "").strip()
            for item in entries
            if isinstance(item, Mapping) and str(item.get("canonical_id") or "").strip()
        }
        compatible_extra_augment_ids: frozenset[str] = frozenset()
        if compatibility_pointer is not None:
            try:
                payload = json.loads(Path(compatibility_pointer).read_text(encoding="utf-8"))
                compatibility_catalog = (
                    load_runtime_catalog_from_pointer(payload)
                    if isinstance(payload, Mapping)
                    else None
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise BlitzRefreshError(
                    "catalog_compatibility_invalid",
                    "待采用 Catalog pointer 无法验证",
                ) from exc
            if compatibility_catalog is None:
                raise BlitzRefreshError(
                    "catalog_compatibility_invalid",
                    "待采用 Catalog pointer 无法打开",
                )
            compatibility_ids = {
                str(int(item["cdragon_id"]))
                for item in load_augment_manifest_entries(compatibility_catalog.root)
                if str(item.get("cdragon_id") or "").lstrip("-").isdigit()
                and int(item["cdragon_id"]) > 0
            }
            compatible_extra_augment_ids = frozenset(compatibility_ids - augment_ids)
        if not augment_ids or not champion_ids or not production_ids:
            raise BlitzRefreshError("catalog_empty")
        return cls(
            generation_id=catalog.generation_id,
            content_sha256=catalog.content_sha256,
            augment_ids=frozenset(augment_ids),
            champion_ids=frozenset(champion_ids),
            production_ids=frozenset(production_ids),
            compatible_extra_augment_ids=compatible_extra_augment_ids,
        )


@dataclass(frozen=True)
class _Response:
    text: str
    status_code: int | None
    error: str
    error_kind: str
    elapsed_ms: int
    attempts: int
    fetched_at: str
    backend: str = "static_http"
    fallback_used: bool = False
    fallback_from: str = ""
    response_headers: Mapping[str, str] | None = None
    not_modified: bool = False
    from_cache: bool = False
    request_key: str = ""

    def attempt(self) -> FetchAttempt:
        failure: FailureKind | None = None
        if self.status_code == 403:
            failure = FailureKind.HTTP_403
        elif self.status_code == 429:
            failure = FailureKind.HTTP_429
        elif self.status_code is not None and 500 <= self.status_code <= 599:
            failure = FailureKind.HTTP_5XX
        elif self.error_kind:
            try:
                failure = FailureKind(self.error_kind)
            except ValueError:
                failure = FailureKind.NETWORK_ERROR
        elif self.status_code not in {200, 304} or not self.text:
            failure = FailureKind.INVALID_PAYLOAD
        return FetchAttempt(
            url=DATA_URL,
            backend=self.backend,
            status_code=self.status_code,
            elapsed_ms=max(0, self.elapsed_ms),
            attempts=max(1, self.attempts),
            failure_kind=failure,
            retryable=failure in {FailureKind.TIMEOUT, FailureKind.TLS_ERROR, FailureKind.NETWORK_ERROR, FailureKind.HTTP_5XX},
            fetched_at=self.fetched_at,
            error=self.error,
        )


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
        max_attempts=2,
        headers=request_headers,
        params=dict(params or {}),
        caller="blitz-mayhem",
        fallback_backend="requests",
        max_response_bytes=MAX_RESPONSE_BYTES,
    )


def _coerce_response(raw: object) -> _Response:
    def value(name: str, default: Any = None) -> Any:
        return raw.get(name, default) if isinstance(raw, Mapping) else getattr(raw, name, default)

    text = value("text", value("html", ""))
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="strict")
    return _Response(
        text=str(text or ""),
        status_code=value("status_code"),
        error=str(value("error", "") or ""),
        error_kind=str(value("error_kind", "") or ""),
        elapsed_ms=int(value("elapsed_ms", 0) or 0),
        attempts=int(value("attempts", 1) or 1),
        fetched_at=str(value("fetched_at", utc_now_iso()) or utc_now_iso()),
        backend=str(value("backend", "static_http") or "static_http"),
        fallback_used=bool(value("fallback_used", False)),
        fallback_from=str(value("fallback_from", "") or ""),
        response_headers=value("response_headers", value("headers")),
        not_modified=bool(value("not_modified", False)),
        from_cache=bool(value("from_cache", False)),
        request_key=str(value("request_key", "") or ""),
    )


def _fetch_response(fetcher: Fetcher) -> _Response:
    response = _coerce_response(fetcher(DATA_URL))
    if response.status_code in {403, 429}:
        raise BlitzRefreshError("blocked", f"Blitz HTTP {response.status_code}", response=response)
    if response.status_code not in {200, 304} or response.error or not response.text:
        raise BlitzRefreshError("fetch_failed", response.error or f"HTTP {response.status_code}", response=response)
    if len(response.text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise BlitzRefreshError("response_too_large", response=response)
    return response


def _fetch_normalized(fetcher: Fetcher) -> tuple[dict[str, Any], _Response]:
    response = _fetch_response(fetcher)
    return _normalize_response(response), response


def _normalize_response(response: _Response) -> dict[str, Any]:
    try:
        return normalize_payload(decode_object(response.text))
    except BlitzSchemaError as exc:
        raise BlitzRefreshError(
            "schema_changed",
            str(exc),
            response=response,
            diagnostics={
                "content_sha256": content_sha256(response.text),
            },
        ) from exc


def _bind_catalog(
    payload: Mapping[str, Any],
    binding: CatalogBinding,
    *,
    coverage_policy: CoveragePolicy,
    previous_record_count: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if coverage_policy not in {"catalog_adoption", "active_partial"}:
        raise SourceRunValidationError(f"未知 Blitz coverage policy：{coverage_policy}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise BlitzRefreshError("schema_changed")
    source_ids = {str(item.get("augment_id") or "") for item in rows if isinstance(item, Mapping)}
    unknown_augments = source_ids - binding.augment_ids
    incompatible_unknown_augments = sorted(
        unknown_augments - binding.compatible_extra_augment_ids,
        key=int,
    )
    compatibility_filtered_augment_ids = sorted(
        unknown_augments & binding.compatible_extra_augment_ids,
        key=int,
    )
    source_champions = {
        str(champion.get("champion_id") or "")
        for item in rows
        if isinstance(item, Mapping)
        for champion in item.get("top_champions", [])
        if isinstance(champion, Mapping)
    }
    unknown_champions = sorted(source_champions - binding.champion_ids, key=int)
    if incompatible_unknown_augments or unknown_champions:
        raise BlitzRefreshError(
            "catalog_binding_failed",
            "Blitz 未知身份："
            f"augments={incompatible_unknown_augments[:20]} champions={unknown_champions[:20]}",
            diagnostics={
                "unknown_augment_count": len(incompatible_unknown_augments),
                "unknown_augment_ids": incompatible_unknown_augments[:20],
                "unknown_champion_count": len(unknown_champions),
                "unknown_champion_ids": unknown_champions[:20],
            },
        )
    filtered_rows = [
        dict(item)
        for item in rows
        if isinstance(item, Mapping)
        and str(item.get("augment_id") or "") not in compatibility_filtered_augment_ids
    ]
    projected = project_normalized_rows(payload, filtered_rows)
    projected_source_ids = {
        str(item.get("augment_id") or "")
        for item in filtered_rows
    }
    covered = projected_source_ids & binding.production_ids
    missing_production_ids = sorted(binding.production_ids - projected_source_ids, key=int)
    ratio = len(covered) / len(binding.production_ids)
    source_record_count = len(source_ids)
    verified_previous_count = (
        int(previous_record_count)
        if previous_record_count is not None and int(previous_record_count) > 0
        else 0
    )
    previous_record_ratio = (
        source_record_count / verified_previous_count if verified_previous_count else None
    )
    required_coverage = (
        MIN_ACTIVE_PRODUCTION_COVERAGE
        if coverage_policy == "active_partial" and verified_previous_count
        else MIN_PRODUCTION_COVERAGE
    )
    if ratio < required_coverage:
        raise BlitzRefreshError(
            "production_coverage_insufficient",
            f"Blitz production coverage={len(covered)}/{len(binding.production_ids)}",
            diagnostics={
                "coverage_policy": coverage_policy,
                "required_coverage_ratio": required_coverage,
                "production_count": len(binding.production_ids),
                "covered_count": len(covered),
                "coverage_ratio": ratio,
                "missing_production_count": len(missing_production_ids),
                "missing_production_ids": missing_production_ids[:20],
            },
        )
    if (
        coverage_policy == "active_partial"
        and verified_previous_count
        and previous_record_ratio is not None
        and previous_record_ratio < MIN_ACTIVE_PREVIOUS_RECORD_RATIO
    ):
        raise BlitzRefreshError(
            "source_record_ratio_insufficient",
            f"Blitz source record ratio={source_record_count}/{verified_previous_count}",
            diagnostics={
                "coverage_policy": coverage_policy,
                "required_previous_record_ratio": MIN_ACTIVE_PREVIOUS_RECORD_RATIO,
                "source_record_count": source_record_count,
                "previous_record_count": verified_previous_count,
                "previous_record_ratio": previous_record_ratio,
                "production_count": len(binding.production_ids),
                "covered_count": len(covered),
                "coverage_ratio": ratio,
            },
        )
    coverage_state = "ready" if ratio >= MIN_PRODUCTION_COVERAGE else "partial_ready"
    return projected, {
        "coverage_state": coverage_state,
        "coverage_policy": coverage_policy,
        "production_count": len(binding.production_ids),
        "covered_count": len(covered),
        "coverage_ratio": ratio,
        "missing_ids": missing_production_ids,
        "source_record_count": source_record_count,
        "filtered_source_record_count": len(projected_source_ids),
        "previous_record_count": verified_previous_count,
        "previous_record_ratio": previous_record_ratio,
        "compatibility_filtered_augment_ids": compatibility_filtered_augment_ids,
    }


def _current_marker(binding: CatalogBinding) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        current = load_source_current("blitz", verify_hash=True)
    except (OSError, SourceRunValidationError, ValueError):
        return {}, {}
    if not current or str(current.get("catalog_generation_id") or "") != binding.generation_id:
        return {}, {}
    try:
        validate_blitz_artifact(current)
    except (OSError, SourceRunValidationError, ValueError):
        return {}, {}
    manifest = load_source_run_manifest("blitz", str(current.get("run_id") or ""))
    marker = manifest.metadata.get("marker") if manifest is not None else None
    return current, dict(marker) if isinstance(marker, Mapping) else {}


def _verified_previous_record_count(current: Mapping[str, Any]) -> int | None:
    """从 hash-verified current artifact 读取上轮原始记录数。

    旧 artifact 没有 coverage 元数据时，退回 pointer 中同样受哈希绑定的
    artifact.record_count；这仍是 verified last-good，但不会伪造额外来源行。
    """

    if not current:
        return None
    payload = validate_blitz_artifact(current)
    coverage = payload.get("coverage")
    if isinstance(coverage, Mapping):
        try:
            value = int(coverage.get("source_record_count") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    artifact = current.get("artifact")
    if isinstance(artifact, Mapping):
        try:
            value = int(artifact.get("record_count") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def _current_reusable(
    current: Mapping[str, Any],
    marker: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    manifest = load_source_run_manifest("blitz", str(current.get("run_id") or ""))
    if manifest is None:
        raise SourceRunValidationError("Blitz current manifest 缺失")
    current_marker = manifest.metadata.get("marker")
    if not isinstance(current_marker, Mapping) or dict(current_marker) != dict(marker):
        return False
    if manifest.metadata.get("parser_revision") != PARSER_REVISION:
        return False
    validate_blitz_artifact(current)
    del now  # Compatibility with callers; age no longer controls content reuse.
    return True


def validate_blitz_artifact(pointer_payload: Mapping[str, Any]) -> dict[str, Any]:
    pointer = SourcePointerV2.from_mapping(pointer_payload)
    if pointer.source != "blitz" or pointer.artifact.role != "augment_ranking":
        raise SourceRunValidationError("Blitz pointer 来源或 artifact role 无效")
    manifest = load_source_run_manifest("blitz", pointer.run_id)
    if manifest is None or manifest.artifact is None or manifest.artifact != pointer.artifact:
        raise SourceRunValidationError("Blitz manifest 与 pointer 不一致")
    manifest_path = source_run_dir("blitz", pointer.run_id) / "manifest.json"
    artifact_path = source_run_artifact_path("blitz", pointer.run_id, pointer.artifact.relative_path)
    if sha256_file(manifest_path) != pointer.manifest_sha256 or sha256_file(artifact_path) != pointer.artifact.sha256:
        raise SourceRunValidationError("Blitz artifact 或 manifest 哈希不一致")
    if artifact_path.stat().st_size != pointer.artifact.size:
        raise SourceRunValidationError("Blitz artifact 大小不一致")
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRunValidationError("Blitz artifact 无法读取") from exc
    normalized = validate_artifact(payload if isinstance(payload, Mapping) else {})
    if len(normalized["rows"]) != pointer.artifact.record_count:
        raise SourceRunValidationError("Blitz artifact record_count 不一致")
    raw_coverage = payload.get("coverage") if isinstance(payload, Mapping) else None
    manifest_coverage = manifest.metadata.get("coverage")
    if raw_coverage is None and manifest_coverage is not None:
        raise SourceRunValidationError("Blitz manifest coverage 缺少 artifact 对应项")
    if raw_coverage is not None:
        if not isinstance(raw_coverage, Mapping):
            raise SourceRunValidationError("Blitz artifact coverage 必须是对象")
        if not isinstance(manifest_coverage, Mapping) or dict(manifest_coverage) != dict(raw_coverage):
            raise SourceRunValidationError("Blitz artifact 与 manifest coverage 不一致")
        normalized["coverage"] = dict(raw_coverage)
    return normalized


def _write_failure(
    *,
    run_id: str,
    binding: CatalogBinding,
    started_at: str,
    reason: str,
    response: _Response | None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    bounded_diagnostics = dict(diagnostics or {})
    if response is not None:
        bounded_diagnostics.update(
            {
                "backend": response.backend,
                "fallback_used": response.fallback_used,
                "fallback_from": response.fallback_from,
            }
        )
    attempt = response.attempt() if response else None
    outcome = ItemOutcome(
        item_id="augment_ranking",
        state="failed",
        stage="fetch" if reason in {"blocked", "fetch_failed", "response_too_large"} else "validation",
        failure_kind=(
            attempt.failure_kind
            if attempt is not None and attempt.failure_kind is not None
            else FailureKind.SCHEMA_CHANGED
        ),
        attempt=attempt,
        details={"reason": reason, **bounded_diagnostics},
    )
    manifest = SourceRunManifestV2(
        source="blitz",
        run_id=run_id,
        catalog_generation_id=binding.generation_id,
        catalog_sha256=binding.content_sha256,
        health=SourceHealth.FAILED,
        started_at=started_at,
        completed_at=utc_now_iso(),
        expected_items=1,
        successful_items=0,
        confirmed_empty_items=0,
        failed_items=1,
        artifact=None,
        outcomes=(outcome,),
        metadata={"reason": reason, "diagnostics": bounded_diagnostics},
    )
    report = {
        "success": False,
        "reason": reason,
        "run_id": run_id,
        "failure_stage": outcome.stage,
        "failure_kind": outcome.failure_kind.value if outcome.failure_kind is not None else "",
        "diagnostics": bounded_diagnostics,
    }
    write_run_diagnostics(manifest, report=report)
    return report


def probe_blitz_upstream_marker(
    *,
    fetcher: Fetcher | None = None,
    conditional_cache_root: Path | None = None,
) -> dict[str, Any]:
    fetch = with_conditional_response(fetcher or _default_fetcher, conditional_cache_root)
    payload, _ = _fetch_normalized(fetch)
    return dict(payload["marker"])


def refresh_blitz(
    *,
    force: bool = False,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
    fetcher: Fetcher | None = None,
    catalog_binding: CatalogBinding | None = None,
    stop_event: Any = None,
    now: datetime | None = None,
    coverage_policy: CoveragePolicy = "catalog_adoption",
    raw_cache_root: Path | None = None,
    conditional_cache_root: Path | None = None,
    previous_failure_fingerprint: str = "",
) -> dict[str, Any]:
    if promote_current:
        raise SourceRunValidationError("正式 source current 只能由 cohort promotion 切换")
    if stop_event is not None and stop_event.is_set():
        raise RuntimeError("blitz_refresh_cancelled")
    binding = catalog_binding or CatalogBinding.active()
    run_id = f"blitz-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started_at = utc_now_iso()
    response: _Response | None = None
    failure_fingerprint = ""
    try:
        fetch = with_conditional_response(
            fetcher or _default_fetcher,
            conditional_cache_root or raw_cache_root,
        )
        response = _fetch_response(fetch)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("blitz_refresh_cancelled")
        failure_fingerprint = failure_identity(
            response.text,
            catalog_generation_id=binding.generation_id,
            catalog_sha256=binding.content_sha256,
        )
        current, previous_marker = _current_marker(binding)
        if (
            not force
            and previous_failure_fingerprint == failure_fingerprint
        ):
            return {
                "success": False,
                "reason": "validation_unchanged",
                "failure_stage": "validation",
                "failure_fingerprint": failure_fingerprint,
                "diagnostics": {
                    "failure_fingerprint": failure_fingerprint,
                    "deduplicated": True,
                },
                "check_status": "unknown",
                "upstream_revision": content_sha256(response.text),
                "applied_revision": str(previous_marker.get("content_sha256") or "") or None,
                "retry_after_seconds": 21600,
            }
        payload = _normalize_response(response)
        upstream_marker = dict(payload["marker"])
        if (
            not force
            and current
            and previous_marker == upstream_marker
            and _current_reusable(
                current,
                upstream_marker,
                now=now or datetime.now(timezone.utc),
            )
        ):
            return {
                "success": True,
                "reason": "not_stale",
                "pointer": current,
                "marker": upstream_marker,
                "check_status": "up_to_date",
                "upstream_revision": str(upstream_marker.get("content_sha256") or ""),
                "applied_revision": str(upstream_marker.get("content_sha256") or ""),
                "upstream_marker": upstream_marker,
                "retry_after_seconds": None,
            }
        previous_record_count = _verified_previous_record_count(current)
        payload, coverage = _bind_catalog(
            payload,
            binding,
            coverage_policy=coverage_policy,
            previous_record_count=previous_record_count,
        )
        artifact_dir = source_run_dir("blitz", run_id) / "augment_ranking"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        artifact_path = artifact_dir / "augment-rankings.v1.json"
        atomic_write_json(artifact_path, {**payload, "coverage": coverage}, ensure_ascii=False, indent=2)
        artifact = build_artifact_descriptor(
            artifact_path,
            role="augment_ranking",
            relative_path="augment_ranking/augment-rankings.v1.json",
            record_count=len(payload["rows"]),
            content_schema_version=1,
        )
        outcome = ItemOutcome(
            item_id="augment_ranking",
            state="success",
            stage="fetch",
            record_count=len(payload["rows"]),
            attempt=response.attempt(),
        )
        manifest = SourceRunManifestV2(
            source="blitz",
            run_id=run_id,
            catalog_generation_id=binding.generation_id,
            catalog_sha256=binding.content_sha256,
            health=SourceHealth.HEALTHY,
            started_at=started_at,
            completed_at=utc_now_iso(),
            expected_items=1,
            successful_items=1,
            confirmed_empty_items=0,
            failed_items=0,
            artifact=artifact,
            outcomes=(outcome,),
            metadata={
                "patch": payload["patch"],
                "data_date": payload["data_date"],
                "marker": upstream_marker,
                "artifact_marker": payload["marker"],
                "parser_revision": PARSER_REVISION,
                "coverage": coverage,
                "compatibility_filtered_augment_ids": coverage[
                    "compatibility_filtered_augment_ids"
                ],
                "fetch_backend": response.backend,
                "fallback_used": response.fallback_used,
                "fallback_from": response.fallback_from,
            },
        )
        report = {
            "success": True,
            "reason": coverage["coverage_state"],
            "run_id": run_id,
            "record_count": len(payload["rows"]),
            "marker": upstream_marker,
            "coverage": coverage,
            "compatibility_filtered_augment_ids": coverage[
                "compatibility_filtered_augment_ids"
            ],
            "fetch_backend": response.backend,
            "fallback_used": response.fallback_used,
            "fallback_from": response.fallback_from,
        }
        pointer = publish_source_run(
            manifest,
            report=report,
            promote_current=False,
            pointer_output=pointer_output,
        )
        validate_blitz_artifact(pointer)
        return {
            **report,
            "pointer": pointer,
            "check_status": "changed",
            "upstream_revision": str(upstream_marker.get("content_sha256") or ""),
            "applied_revision": str(upstream_marker.get("content_sha256") or ""),
            "upstream_marker": upstream_marker,
            "retry_after_seconds": None,
        }
    except (BlitzRefreshError, BlitzSchemaError, SourceRunValidationError, OSError, ValueError) as exc:
        if isinstance(exc, BlitzRefreshError) and isinstance(exc.response, _Response):
            response = exc.response
        reason = exc.reason if isinstance(exc, BlitzRefreshError) else (
            "schema_changed" if isinstance(exc, BlitzSchemaError) else "publish_failed"
        )
        validation_contract_failure = reason in {
            "schema_changed",
            "catalog_binding_failed",
            "production_coverage_insufficient",
            "source_record_ratio_insufficient",
        }
        diagnostics = exc.diagnostics if isinstance(exc, BlitzRefreshError) else {
            "error_type": exc.__class__.__name__,
        }
        if response is not None and validation_contract_failure and not failure_fingerprint:
            failure_fingerprint = failure_identity(
                response.text,
                catalog_generation_id=binding.generation_id,
                catalog_sha256=binding.content_sha256,
            )
        if failure_fingerprint and validation_contract_failure:
            diagnostics = {**diagnostics, "failure_fingerprint": failure_fingerprint}
        report = _write_failure(
            run_id=run_id,
            binding=binding,
            started_at=started_at,
            reason=reason,
            response=response,
            diagnostics=diagnostics,
        )
        retry_after = (
            parse_retry_after_seconds(response.response_headers)
            if response is not None and response.attempt().failure_kind is not None
            else None
        )
        return {
            **report,
            "check_status": "unknown",
            "failure_fingerprint": failure_fingerprint if validation_contract_failure else "",
            "upstream_revision": (
                str(locals()["upstream_marker"].get("content_sha256") or "")
                if isinstance(locals().get("upstream_marker"), Mapping)
                else (content_sha256(response.text) if response is not None else "")
            ),
            "applied_revision": (
                str(locals()["previous_marker"].get("content_sha256") or "") or None
                if isinstance(locals().get("previous_marker"), Mapping)
                else None
            ),
            "retry_after_seconds": 21600 if validation_contract_failure else retry_after,
        }


__all__ = [
    "BlitzRefreshError",
    "CatalogBinding",
    "DATA_URL",
    "MAX_RESPONSE_BYTES",
    "MIN_ACTIVE_PREVIOUS_RECORD_RATIO",
    "MIN_ACTIVE_PRODUCTION_COVERAGE",
    "MIN_PRODUCTION_COVERAGE",
    "probe_blitz_upstream_marker",
    "refresh_blitz",
    "validate_blitz_artifact",
]
