"""Mayhem 低频联动刷新 helper。

本模块只负责 ARAMMayhem 公开 combo 数据的低频增量更新：
- 不调用 ApexLoL 抓取器，不读取浏览器、cookie 或代理配置。
- 抓取 raw 先发布到 runtime cache，再通过 cleaned 合并脚本做 schema/闭集校验。
- 失败只写诊断状态，保留旧 ``Champion_Synergy_Cleaned.json``。

调用方: core.refresh、dev_checks; 关键依赖: catalog.runtime_store、overlay.hints、scraping.synergy.mayhem_combo_scraper。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.catalog.runtime_store import build_runtime_cache_path, build_runtime_state_path
from hextech.overlay.hints import build_overlay_hint_cache_from_precomputed, write_overlay_hint_cache
from hextech.scraping.synergy.mayhem_combo_scraper import scrape_mayhem_combos
from hextech.support.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

MAYHEM_STALE_SECONDS = 72 * 60 * 60
MAYHEM_RAW_CACHE_FILENAME = "mayhem_combos.raw.json"
MAYHEM_REFRESH_STATUS_FILENAME = "mayhem_refresh_status.json"


def _now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).isoformat(timespec="seconds")


def get_mayhem_raw_cache_path() -> str:
    return build_runtime_cache_path(MAYHEM_RAW_CACHE_FILENAME)


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


def mayhem_refresh_due(*, now: float | None = None, stale_after_seconds: int = MAYHEM_STALE_SECONDS) -> bool:
    status = load_mayhem_refresh_status()
    reference = _parse_timestamp(status.get("last_success_at") or status.get("last_attempt_at"))
    if reference <= 0:
        return True
    current = time.time() if now is None else now
    return (reference + stale_after_seconds) <= current


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


def _rebuild_overlay_hint_cache() -> None:
    cache_payload = build_overlay_hint_cache_from_precomputed(source_tag="mayhem-refresh")
    write_overlay_hint_cache(cache_payload)


def run_mayhem_refresh(
    *,
    force: bool = False,
    now: float | None = None,
    scraper: Callable[[], Mapping[str, Any]] | None = None,
    merge: Callable[..., Mapping[str, Any]] | None = None,
    rebuild_hint_cache: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """按 72 小时 stale 阈值刷新 Mayhem cleaned 数据，失败不影响旧 cleaned。"""

    current = time.time() if now is None else now
    if not force and not mayhem_refresh_due(now=current):
        return write_mayhem_refresh_status(
            result="skipped",
            reason="not_stale",
            now=current,
            extra={"stale_after_seconds": MAYHEM_STALE_SECONDS},
        )

    try:
        fetch = scraper or (lambda: scrape_mayhem_combos(max_pages=0))
        raw_payload = dict(fetch())
        raw_items = _raw_item_count(raw_payload)
        if raw_items <= 0:
            return write_mayhem_refresh_status(
                result="failed",
                reason="raw_empty",
                raw_items=raw_items,
                now=current,
            )

        raw_path = Path(get_mayhem_raw_cache_path())
        atomic_write_json(raw_path, raw_payload, ensure_ascii=False, indent=2)

        merge_func = merge
        if merge_func is None:
            from hextech.scraping.synergy.mayhem_merge import merge_mayhem_combos

            merge_func = merge_mayhem_combos
        summary = dict(merge_func(mayhem_raw_path=raw_path, write_output=True))
        added_items = int(summary.get("added_items") or 0)
        if not summary.get("written"):
            return write_mayhem_refresh_status(
                result="failed",
                reason="cleaned_not_written",
                raw_items=raw_items,
                added_items=added_items,
                now=current,
                extra={"summary": summary},
            )

        (rebuild_hint_cache or _rebuild_overlay_hint_cache)()
        return write_mayhem_refresh_status(
            result="success",
            reason="",
            raw_items=raw_items,
            added_items=added_items,
            now=current,
            extra={"summary": summary},
        )
    except Exception as exc:
        logger.exception("Mayhem 低频刷新失败")
        return write_mayhem_refresh_status(
            result="failed",
            reason=f"{type(exc).__name__}: {exc}",
            now=current,
        )


__all__ = [
    "MAYHEM_RAW_CACHE_FILENAME",
    "MAYHEM_REFRESH_STATUS_FILENAME",
    "MAYHEM_STALE_SECONDS",
    "get_mayhem_raw_cache_path",
    "get_mayhem_refresh_status_path",
    "load_mayhem_refresh_status",
    "mayhem_refresh_due",
    "run_mayhem_refresh",
    "write_mayhem_refresh_status",
]
