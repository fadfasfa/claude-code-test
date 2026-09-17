"""Mayhem 低频联动刷新 helper。

本模块只负责 ARAMMayhem 公开 combo 数据的低频增量更新：
- 不调用 ApexLoL 抓取器，不读取浏览器、cookie 或代理配置。
- 抓取 raw 先发布到 runtime cache，再通过 cleaned 合并脚本做 schema/闭集校验。
- 失败只写诊断状态，保留已发布的 Apex/Mayhem source current。

调用方: core.refresh、dev_checks; 关键依赖: catalog.runtime_store、overlay.hints、scraping.synergy.mayhem_combo_scraper。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.contracts import ArtifactDescriptor
from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.infrastructure.transport.conditional_response import ConditionalResponseCache
from hextech.infrastructure.sources.mayhem.source import scrape_mayhem_combos
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import (
    load_source_current,
    load_source_run_manifest,
    resolve_current_artifact,
    source_run_artifact_path,
)
from hextech.modules.acquisition.common.contracts import utc_now_iso
from hextech.modules.acquisition.mayhem.diagnostics import summarize_rejects
from hextech.infrastructure.sources.mayhem.publisher import publish_mayhem_run
from hextech.modules.data.catalog.versioned import load_active_catalog

logger = logging.getLogger(__name__)

MAYHEM_STALE_SECONDS = 4 * 60 * 60
MAYHEM_FAILURE_RETRY_SECONDS = 30 * 60
MAYHEM_FAILURE_RETRY_JITTER_SECONDS = 5 * 60
MAYHEM_REPEAT_INVALID_RETRY_SECONDS = 6 * 60 * 60
MAYHEM_PROJECTION_REVISION = "mayhem-projection-v2"
MAYHEM_RAW_CACHE_FILENAME = "mayhem_combos.raw.json"
MAYHEM_REFRESH_STATUS_FILENAME = "mayhem_refresh_status.json"


def _now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).isoformat(timespec="seconds")


def get_mayhem_raw_cache_path() -> str:
    artifact = resolve_current_artifact("mayhem")
    return str(artifact or source_run_artifact_path("mayhem", "pending", "combos.json"))


def get_mayhem_refresh_status_path() -> str:
    return build_runtime_state_path(MAYHEM_REFRESH_STATUS_FILENAME)


def load_mayhem_refresh_status() -> dict[str, Any]:
    try:
        payload = json.loads(Path(get_mayhem_refresh_status_path()).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _stable_failure_retry_jitter_seconds(status: Mapping[str, Any], jitter_seconds: int) -> int:
    """按失败状态生成稳定抖动，避免持续失败时形成精确固定请求节奏。"""

    jitter = max(0, int(jitter_seconds or 0))
    if jitter <= 0:
        return 0
    seed = "|".join(
        str(status.get(key) or "")
        for key in ("last_attempt_at", "last_result", "reason")
    )
    digest = hashlib.blake2b(seed.encode("utf-8", errors="replace"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % (jitter + 1)


def mayhem_refresh_due(
    *,
    now: float | None = None,
    stale_after_seconds: int = MAYHEM_STALE_SECONDS,
    failure_retry_seconds: int = MAYHEM_FAILURE_RETRY_SECONDS,
    failure_retry_jitter_seconds: int = MAYHEM_FAILURE_RETRY_JITTER_SECONDS,
) -> bool:
    status = load_mayhem_refresh_status()
    current = time.time() if now is None else now
    success_at = _parse_timestamp(status.get("last_success_at"))
    if success_at <= 0:
        attempt_at = _parse_timestamp(status.get("last_attempt_at"))
        if attempt_at > 0 and str(status.get("last_result") or "") != "success":
            retry_after = max(0, int(failure_retry_seconds)) + _stable_failure_retry_jitter_seconds(
                status,
                failure_retry_jitter_seconds,
            )
            return (attempt_at + retry_after) <= current
        return True
    return (success_at + stale_after_seconds) <= current


def write_mayhem_refresh_status(
    *,
    result: str,
    reason: str = "",
    raw_items: int = 0,
    added_items: int = 0,
    now: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous = load_mayhem_refresh_status()
    timestamp = _now_iso(now)
    payload: dict[str, Any] = {
        "last_attempt_at": timestamp,
        "last_success_at": previous.get("last_success_at", ""),
        "last_result": result,
        "reason": reason,
        "raw_items": int(raw_items or 0),
        "added_items": int(added_items or 0),
    }
    if result == "success":
        payload["last_success_at"] = timestamp
    if extra:
        payload.update(dict(extra))
    atomic_write_json(get_mayhem_refresh_status_path(), payload, ensure_ascii=False, indent=2)
    return payload


def _raw_item_count(payload: Mapping[str, Any]) -> int:
    items = payload.get("items")
    return len(items) if isinstance(items, list) else 0


def _semantic_revision(payload: Mapping[str, Any]) -> str:
    comparable = {
        "schema_version": payload.get("schema_version"),
        "source": payload.get("source"),
        "source_url": payload.get("source_url"),
        "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
    }
    encoded = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_manifest() -> tuple[dict[str, Any], Mapping[str, Any]]:
    try:
        pointer = load_source_current("mayhem")
        manifest = load_source_run_manifest("mayhem", str(pointer.get("run_id") or ""))
    except (OSError, TypeError, ValueError):
        return {}, {}
    return pointer, dict(manifest.metadata) if manifest is not None else {}


def _result_fields(
    *,
    check_status: str,
    upstream_revision: str,
    applied_revision: str = "",
    retry_after_seconds: int = 0,
) -> dict[str, Any]:
    representation_revision = upstream_revision
    semantic_upstream = (
        applied_revision
        if check_status in {"up_to_date", "changed"} and applied_revision
        else upstream_revision
    )
    return {
        "check_status": check_status,
        "upstream_revision": semantic_upstream,
        "applied_revision": applied_revision,
        "representation_revision": representation_revision,
        "retry_after_seconds": max(0, int(retry_after_seconds)),
    }


def _failure_fingerprint(
    upstream_revision: str,
    parser_revision: str,
    catalog_generation_id: str,
    catalog_sha256: str,
    *,
    validation_input: Mapping[str, Any],
) -> str:
    # Successful reuse compares business rows. Failed validation must additionally
    # distinguish repaired rejects/pagination, even when those rows stay identical.
    inputs = {
        key: validation_input.get(key)
        for key in (
            "schema_version", "source", "source_url", "manifest_url", "items",
            "rejects", "page", "max_pages",
        )
    }
    transport = validation_input.get("transport")
    inputs["check_complete"] = transport.get("check_complete") if isinstance(transport, Mapping) else None
    encoded = json.dumps(
        {
            "identity_version": 2,
            "upstream_revision": upstream_revision,
            "validation_input": inputs,
            "parser_revision": parser_revision,
            "catalog_generation_id": catalog_generation_id,
            "catalog_sha256": catalog_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retry_after(transport: Mapping[str, Any], default: int) -> int:
    try:
        server_value = int(transport.get("retry_after_seconds") or 0)
    except (TypeError, ValueError):
        server_value = 0
    return max(default, server_value)


def _merge_candidate(
    raw_payload: Mapping[str, Any],
    *,
    catalog_root: Path,
    merge: Callable[..., Mapping[str, Any]] | None,
) -> dict[str, Any]:
    augment_path = catalog_root / "海克斯资源目录.v1.json"
    core_path = catalog_root / "英雄目录.v1.json"
    if merge is None:
        from hextech.modules.acquisition.mayhem.merge import merge_mayhem_payloads

        manifest_payload = json.loads(augment_path.read_text(encoding="utf-8"))
        core_payload = json.loads(core_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_payload, list) or not isinstance(core_payload, dict):
            raise ValueError("Mayhem Catalog 投影输入无效")
        return dict(merge_mayhem_payloads(
            apex_payload={},
            mayhem_payload=dict(raw_payload),
            manifest_payload=manifest_payload,
            core_payload=core_payload,
        ))
    with tempfile.TemporaryDirectory(prefix="hextech-mayhem-") as temporary:
        raw_path = Path(temporary) / "mayhem.raw.json"
        atomic_write_json(raw_path, dict(raw_payload), ensure_ascii=False, indent=2)
        return dict(merge(
            mayhem_raw_path=raw_path,
            augment_manifest_path=augment_path,
            core_data_path=core_path,
            write_output=False,
            validate_only=True,
        ))


def _failure_status(
    *,
    reason: str,
    raw_items: int = 0,
    added_items: int = 0,
    now: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return write_mayhem_refresh_status(
        result="failed",
        reason=reason,
        raw_items=raw_items,
        added_items=added_items,
        now=now,
        extra=extra,
    )


def run_mayhem_refresh(
    *,
    force: bool = False,
    now: float | None = None,
    scraper: Callable[[], Mapping[str, Any]] | None = None,
    merge: Callable[..., Mapping[str, Any]] | None = None,
    promote_current: bool = False,
    pointer_output: str | os.PathLike[str] | None = None,
    conditional_cache_root: str | os.PathLike[str] | None = None,
    parser_revision: str = MAYHEM_PROJECTION_REVISION,
    previous_failure_fingerprint: str = "",
) -> dict[str, Any]:
    """检查 Mayhem 上游，且仅在业务内容或投影绑定变化时发布。"""

    current = time.time() if now is None else now
    if not force and not mayhem_refresh_due(now=current):
        return write_mayhem_refresh_status(
            result="skipped",
            reason="not_stale",
            now=current,
            extra={
                "success": True,
                "stale_after_seconds": MAYHEM_STALE_SECONDS,
                **_result_fields(check_status="unknown", upstream_revision=""),
            },
        )

    raw_items = 0
    added_items = 0
    started_at = utc_now_iso()
    run_id = f"mayhem-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        conditional_cache = (
            ConditionalResponseCache(Path(conditional_cache_root), source="mayhem")
            if conditional_cache_root is not None
            else None
        )
        fetch = scraper or (
            lambda: scrape_mayhem_combos(max_pages=0, conditional_cache=conditional_cache)
        )
        raw_payload = dict(fetch())
        upstream_revision = str(raw_payload.get("upstream_revision") or "")
        transport = dict(raw_payload.get("transport") or {})
        catalog = load_active_catalog()
        pointer, current_metadata = _current_manifest()
        current_applied = str(current_metadata.get("applied_revision") or "")
        projection_matches = (
            bool(pointer)
            and str(pointer.get("catalog_generation_id") or "") == catalog.generation_id
            and str(pointer.get("catalog_sha256") or "") == catalog.content_sha256
            and str(current_metadata.get("parser_revision") or "") == parser_revision
        )
        failure_fingerprint = _failure_fingerprint(
            upstream_revision,
            parser_revision,
            catalog.generation_id,
            catalog.content_sha256,
            validation_input=raw_payload,
        )
        failure_identity = {
            "success": False,
            "failure_fingerprint": failure_fingerprint,
            "failure_parser_revision": parser_revision,
            "failure_catalog_generation_id": catalog.generation_id,
            "failure_catalog_sha256": catalog.content_sha256,
            "transport": transport,
            **_result_fields(
                check_status="unknown",
                upstream_revision=upstream_revision,
                applied_revision=current_applied,
                retry_after_seconds=_retry_after(transport, 5 * 60),
            ),
        }
        raw_items = _raw_item_count(raw_payload)
        if transport.get("check_complete") is False:
            return _failure_status(
                reason="upstream_check_incomplete",
                raw_items=raw_items,
                now=current,
                extra=failure_identity,
            )
        if raw_items <= 0:
            previous_status = load_mayhem_refresh_status()
            known_failure = previous_failure_fingerprint or str(
                previous_status.get("failure_fingerprint") or ""
            )
            repeated = bool(failure_fingerprint) and (
                failure_fingerprint == known_failure
            )
            return _failure_status(
                reason="repeated_invalid_content" if repeated else "raw_empty",
                raw_items=raw_items,
                now=current,
                extra={
                    **failure_identity,
                    "retry_after_seconds": (
                        _retry_after(
                            transport,
                            MAYHEM_REPEAT_INVALID_RETRY_SECONDS if repeated else 5 * 60,
                        )
                    ),
                },
            )

        if (
            projection_matches
            and upstream_revision
            and upstream_revision == str(current_metadata.get("upstream_revision") or "")
        ):
            return write_mayhem_refresh_status(
                result="success",
                reason="not_stale",
                raw_items=raw_items,
                now=current,
                extra={
                    "success": True,
                    "transport": transport,
                    **_result_fields(
                        check_status="up_to_date",
                        upstream_revision=upstream_revision,
                        applied_revision=current_applied,
                    ),
                },
            )

        previous_status = load_mayhem_refresh_status()
        known_failure = previous_failure_fingerprint or str(
            previous_status.get("failure_fingerprint") or ""
        )
        repeated_failure = bool(failure_fingerprint) and (
            failure_fingerprint == known_failure
        )
        if repeated_failure:
            return _failure_status(
                reason="repeated_invalid_content",
                raw_items=raw_items,
                now=current,
                extra={
                    "success": False,
                    "failure_fingerprint": failure_fingerprint,
                    "failure_parser_revision": parser_revision,
                    "failure_catalog_generation_id": catalog.generation_id,
                    "failure_catalog_sha256": catalog.content_sha256,
                    "transport": transport,
                    **_result_fields(
                        check_status="unknown",
                        upstream_revision=upstream_revision,
                        applied_revision=current_applied,
                        retry_after_seconds=_retry_after(
                            transport, MAYHEM_REPEAT_INVALID_RETRY_SECONDS
                        ),
                    ),
                },
            )

        rejects = raw_payload.get("rejects") if isinstance(raw_payload.get("rejects"), list) else []
        reject_report = summarize_rejects(item for item in rejects if isinstance(item, Mapping))
        reject_ratio = reject_report["count"] / max(1, raw_items + reject_report["count"])
        max_reject_ratio = float(os.getenv("MAYHEM_MAX_REJECT_RATIO", "0.15") or "0.15")
        if reject_ratio > max_reject_ratio:
            return _failure_status(
                reason="reject_ratio_exceeded",
                raw_items=raw_items,
                now=current,
                extra={**failure_identity, "reject_ratio": reject_ratio, "reject_report": reject_report},
            )

        previous_descriptor = ArtifactDescriptor.from_mapping(pointer["artifact"]) if pointer else None
        previous_count = previous_descriptor.record_count if previous_descriptor is not None else 0
        if previous_count and raw_items < max(1, previous_count // 2):
            return _failure_status(
                reason="scale_regression",
                raw_items=raw_items,
                now=current,
                extra={**failure_identity, "previous_items": previous_count},
            )

        summary = _merge_candidate(raw_payload, catalog_root=catalog.root, merge=merge)
        added_items = int(summary.get("added_items") or 0)
        if int(summary.get("mayhem_valid_items") or 0) <= 0:
            return _failure_status(
                reason="no_valid_combos",
                raw_items=raw_items,
                added_items=added_items,
                now=current,
                extra={**failure_identity, "summary": summary},
            )

        page = raw_payload.get("page") if isinstance(raw_payload.get("page"), Mapping) else {}
        normalized_payload = {
            "schema_version": 2,
            "source": "arammayhem",
            "source_url": str(raw_payload.get("source_url") or ""),
            "fetched_at": str(raw_payload.get("fetched_at") or started_at),
            "items": list(summary.get("normalized_items") or []),
        }
        applied_revision = _semantic_revision(normalized_payload)
        if projection_matches and current_applied and applied_revision == current_applied:
            return write_mayhem_refresh_status(
                result="success",
                reason="not_stale",
                raw_items=raw_items,
                added_items=added_items,
                now=current,
                extra={
                    "success": True,
                    "transport": transport,
                    **_result_fields(
                        check_status="up_to_date",
                        upstream_revision=upstream_revision,
                        applied_revision=applied_revision,
                    ),
                },
            )
        normalized_rejects = [
            {"reason_code": str(item.get("reason") or "unknown"), **dict(item)}
            for item in (list(raw_payload.get("rejects") or []) + list(summary.get("clean_rejects") or []))
            if isinstance(item, Mapping)
        ]
        validation_report = {
            "raw_items": int(summary.get("mayhem_raw_items") or 0),
            "valid_items": int(summary.get("mayhem_valid_items") or 0),
            "duplicate_items": int(summary.get("skipped_duplicate_items") or 0),
            "rejected_items": int(summary.get("reject_items") or 0),
            "rejects": normalized_rejects[:20],
            "max_pages": int(raw_payload.get("max_pages") or 0),
            "parse_mode": str(page.get("parse_mode") or ""),
            "selected": int(page.get("selected") or 0),
            "total": int(page.get("total") or 0),
            "pagination_complete": page.get("pagination_complete") is True,
            "merge_dry_run": {key: value for key, value in summary.items() if key not in {"merged_payload", "normalized_items"}},
        }
        source_data_at = (
            str(current_metadata.get("data_at") or "")
            if upstream_revision == str(current_metadata.get("upstream_revision") or "")
            else ""
        ) or str(raw_payload.get("fetched_at") or started_at)
        published_path, _ = publish_mayhem_run(
            normalized_payload,
            run_id=run_id,
            started_at=started_at,
            report=validation_report,
            data_at=source_data_at,
            upstream_revision=upstream_revision,
            applied_revision=applied_revision,
            parser_revision=parser_revision,
            promote_current=promote_current,
            pointer_output=pointer_output,
        )
        return write_mayhem_refresh_status(
            result="success",
            reason="",
            raw_items=raw_items,
            added_items=added_items,
            now=current,
            extra={
                "success": True,
                "summary": summary,
                "source_artifact": published_path,
                "run_id": run_id,
                "transport": transport,
                **_result_fields(
                    check_status="changed",
                    upstream_revision=upstream_revision,
                    applied_revision=applied_revision,
                ),
            },
        )
    except Exception as exc:
        logger.exception("Mayhem 低频刷新失败")
        return _failure_status(
            reason=f"{type(exc).__name__}: {exc}",
            raw_items=raw_items,
            added_items=added_items,
            now=current,
            extra={
                "success": False,
                **_result_fields(
                    check_status="unknown",
                    upstream_revision=locals().get("upstream_revision", ""),
                    applied_revision=locals().get("current_applied", ""),
                    retry_after_seconds=_retry_after(
                        locals().get("transport", {}), 5 * 60
                    ),
                ),
            },
        )


__all__ = [
    "MAYHEM_RAW_CACHE_FILENAME",
    "MAYHEM_REFRESH_STATUS_FILENAME",
    "MAYHEM_FAILURE_RETRY_JITTER_SECONDS",
    "MAYHEM_STALE_SECONDS",
    "get_mayhem_raw_cache_path",
    "get_mayhem_refresh_status_path",
    "load_mayhem_refresh_status",
    "mayhem_refresh_due",
    "run_mayhem_refresh",
    "write_mayhem_refresh_status",
]
