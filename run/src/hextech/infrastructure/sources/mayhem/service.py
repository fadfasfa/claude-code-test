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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.contracts import ArtifactDescriptor
from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.infrastructure.sources.mayhem.source import scrape_mayhem_combos
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import load_source_current, resolve_current_artifact, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import utc_now_iso
from hextech.modules.acquisition.mayhem.diagnostics import summarize_rejects
from hextech.infrastructure.sources.mayhem.publisher import publish_mayhem_run
from hextech.modules.data.catalog.versioned import load_active_catalog

logger = logging.getLogger(__name__)

MAYHEM_STALE_SECONDS = 72 * 60 * 60
MAYHEM_FAILURE_RETRY_SECONDS = 30 * 60
MAYHEM_FAILURE_RETRY_JITTER_SECONDS = 5 * 60
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


def _rebuild_overlay_hint_cache() -> None:
    """兼容旧注入点；generation 只由 DataService 在完整构建后发布。"""


def run_mayhem_refresh(
    *,
    force: bool = False,
    now: float | None = None,
    scraper: Callable[[], Mapping[str, Any]] | None = None,
    merge: Callable[..., Mapping[str, Any]] | None = None,
    rebuild_hint_cache: Callable[[], None] | None = None,
    promote_current: bool = False,
    pointer_output: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """按 stale 阈值刷新 Mayhem 来源；失败不影响上一份 current。"""

    current = time.time() if now is None else now
    if not force and not mayhem_refresh_due(now=current):
        return write_mayhem_refresh_status(
            result="skipped",
            reason="not_stale",
            now=current,
            extra={"stale_after_seconds": MAYHEM_STALE_SECONDS},
        )

    raw_items = 0
    added_items = 0
    started_at = utc_now_iso()
    run_id = f"mayhem-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        fetch = scraper or (lambda: scrape_mayhem_combos(max_pages=0))
        raw_payload = dict(fetch())
        raw_items = _raw_item_count(raw_payload)
        if raw_items <= 0:
            return _failure_status(
                reason="raw_empty",
                raw_items=raw_items,
                now=current,
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
                extra={"reject_ratio": reject_ratio, "reject_report": reject_report},
            )

        previous = load_source_current("mayhem")
        previous_descriptor = ArtifactDescriptor.from_mapping(previous["artifact"]) if previous else None
        previous_count = previous_descriptor.record_count if previous_descriptor is not None else 0
        if previous_count and raw_items < max(1, previous_count // 2):
            return _failure_status(
                reason="scale_regression",
                raw_items=raw_items,
                now=current,
                extra={"previous_items": previous_count},
            )

        raw_path = source_run_artifact_path("mayhem", run_id, "combos.json")
        atomic_write_json(raw_path, raw_payload, ensure_ascii=False, indent=2)

        merge_func = merge
        if merge_func is None:
            from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

            merge_func = merge_mayhem_combos
        catalog = load_active_catalog()
        summary = dict(
            merge_func(
                mayhem_raw_path=raw_path,
                augment_manifest_path=catalog.root / "海克斯资源目录.v1.json",
                core_data_path=catalog.root / "英雄目录.v1.json",
                write_output=False,
                validate_only=True,
            )
        )
        added_items = int(summary.get("added_items") or 0)
        if int(summary.get("mayhem_valid_items") or 0) <= 0:
            return _failure_status(
                reason="no_valid_combos",
                raw_items=raw_items,
                added_items=added_items,
                now=current,
                extra={"summary": summary},
            )

        page = raw_payload.get("page") if isinstance(raw_payload.get("page"), Mapping) else {}
        normalized_payload = {
            "schema_version": 2,
            "source": "arammayhem",
            "source_url": str(raw_payload.get("source_url") or ""),
            "fetched_at": str(raw_payload.get("fetched_at") or started_at),
            "items": list(summary.get("normalized_items") or []),
        }
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
        published_path, _ = publish_mayhem_run(
            normalized_payload,
            run_id=run_id,
            started_at=started_at,
            report=validation_report,
            promote_current=promote_current,
            pointer_output=pointer_output,
        )
        return write_mayhem_refresh_status(
            result="success",
            reason="",
            raw_items=raw_items,
            added_items=added_items,
            now=current,
            extra={"summary": summary, "source_artifact": published_path, "run_id": run_id},
        )
    except Exception as exc:
        logger.exception("Mayhem 低频刷新失败")
        return _failure_status(
            reason=f"{type(exc).__name__}: {exc}",
            raw_items=raw_items,
            added_items=added_items,
            now=current,
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
