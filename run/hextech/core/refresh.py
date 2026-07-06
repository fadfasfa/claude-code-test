"""运行时编排层。

文件职责：
- 收敛抓取、自愈、预计算缓存重建和启动状态判断等后台编排入口

核心输入：
- 当前运行目录中的核心配置、CSV 和自愈状态

核心输出：
- 统一的后台刷新入口与就绪状态判断结果

主要依赖：
- `hextech.scraping.*`
- `hextech.catalog.precomputed_cache`

维护提醒：
- 上层只应调用这里暴露的编排入口，不应在 UI 或 Web 中直接拼装多段抓取流程

调用方: display.desktop.app、display.web.api、display.web.runtime; 关键依赖: catalog.runtime_store、support.atomic_io、support.log_utils。
"""

from __future__ import annotations

import os
import re
import time
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from hextech.catalog.runtime_store import (
    build_runtime_state_path,
    build_synergy_data_path,
    build_synergy_latest_pointer_path,
    build_synergy_refresh_status_path,
    ensure_private_runtime_dir,
    get_latest_synergy_snapshot_path,
    get_latest_csv,
    get_latest_valid_csv,
    load_synergy_latest_pointer,
    load_synergy_refresh_status,
)
from hextech.support.atomic_io import atomic_write_json
from hextech.support.log_utils import write_structured_event
from hextech.scraping.hextech.scraper import hextech_refresh_blocked, main_scraper
from hextech.scraping.synergy.scraper import main as run_apex_spider
from hextech.scraping.synergy.scraper import SYNERGY_REFRESH_META_VERSION
from hextech.catalog.precomputed_cache import (
    has_precomputed_hextech_cache,
    load_precomputed_champion_list,
    rebuild_precomputed_api_cache_from_latest_csv,
)
from hextech.scraping.augment_catalog import (
    is_augment_icon_prefetch_ready,
    manifest_has_incomplete_entries,
    run_augment_icon_prefetch,
)
from hextech.scraping.version_sync import (
    AUGMENT_ICON_FILE,
    AUGMENT_MANIFEST_FILE,
    AUGMENT_MAP_FILE,
    CORE_DATA_FILE,
    sync_hero_data,
)


SYNERGY_FILE = build_synergy_data_path()
logger = logging.getLogger(__name__)
HIGH_FREQUENCY_STALE_SECONDS = 4 * 60 * 60
SYNERGY_STALE_SECONDS = 7 * 24 * 60 * 60
SYNERGY_BLOCKED_COOLDOWN_SECONDS = 6 * 60 * 60
RUNTIME_EVENT_SCHEMA_VERSION = 1
RUNTIME_EVENT_LOG_FILENAME = "runtime_events.v1.jsonl"
_PUBLISHER_INSTANCE_ID = os.getenv("HEXTECH_COMPONENT_INSTANCE_ID") or f"refresh-{os.getpid()}-{uuid.uuid4().hex[:8]}"
_ACTIVE_DEGRADATION: dict[str, object] = {}


@dataclass(frozen=True)
class RefreshResult:
    """后台刷新结构化结果，避免把 fallback 误报为普通成功。"""

    state: str
    remote_success: bool
    fallback_used: bool
    fallback_valid: bool
    published: bool
    published_data_path: str
    data_version: str
    data_hash: str
    reason_code: str
    correlation_id: str
    degradation_id: str
    report: dict

    def __bool__(self) -> bool:
        """兼容旧调用方的 bool 判断；degraded 代表可服务但不是 ready。"""

        return self.state in {"ready", "degraded"}


def auto_synergy_refresh_enabled() -> bool:
    env_enabled = os.getenv("HEXTECH_AUTO_SYNERGY_REFRESH", "0").strip().lower() in {"1", "true", "yes", "on"}
    # ApexLoL 自动协同刷新仍处于退役状态；Mayhem 增量通过手动清洗脚本生成 cleaned 数据。
    return False and env_enabled


def sanitize_event_message(value: object) -> str:
    """写结构化事件前剥离凭据、cookie、nonce 和 URL query。"""

    text = str(value or "")
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"Authorization:\s*Bearer\s+[^\s,;]+", "Authorization: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"Set-Cookie:\s*[^,\n;]+(?:;[^\n,]*)?", "Set-Cookie: <redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(cookie|token|nonce|api[_-]?key|authorization)=([^,\s;]+)", r"\1=<redacted>", text, flags=re.IGNORECASE)
    return text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_event_log_path() -> Path:
    return Path(build_runtime_state_path(RUNTIME_EVENT_LOG_FILENAME))


def _safe_file_hash(path: str) -> str:
    """返回轻量文件指纹，用于区分同版本文件被替换的情况。"""

    if not path or not os.path.exists(path):
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"size={stat.st_size};mtime_ns={stat.st_mtime_ns}"


def _data_version_from_path(path: str) -> str:
    basename = os.path.basename(str(path or ""))
    match = re.search(r"Hextech_Data_(\d{4}-?\d{2}-?\d{2})", basename)
    return match.group(1) if match else basename


def _append_runtime_event(event: dict) -> None:
    target = _runtime_event_log_path()
    ensure_private_runtime_dir(target.parent)
    payload = dict(event)
    component = str(payload.pop("component", "refresh") or "refresh")
    event_name = str(payload.pop("event", "runtime.event") or "runtime.event")
    payload.setdefault("schema_version", RUNTIME_EVENT_SCHEMA_VERSION)
    payload.setdefault("timestamp", _utc_now_iso())
    payload.setdefault("level", "INFO")
    payload.setdefault("supervisor_instance_id", os.getenv("HEXTECH_SUPERVISOR_INSTANCE_ID", "standalone"))
    payload.setdefault("component_instance_id", os.getenv("HEXTECH_REFRESH_INSTANCE_ID", _PUBLISHER_INSTANCE_ID))
    payload.setdefault("publisher_instance_id", _PUBLISHER_INSTANCE_ID)
    payload.setdefault("generation", int(os.getenv("HEXTECH_REFRESH_GENERATION", "1") or "1"))
    if "error_message_sanitized" in payload:
        payload["error_message_sanitized"] = sanitize_event_message(payload.get("error_message_sanitized"))
    write_structured_event(component, event_name, target_path=target, **payload)


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


def _new_degradation_id() -> str:
    return f"deg-{uuid.uuid4().hex}"


def _file_is_fresh(path: str, stale_after_seconds: int = HIGH_FREQUENCY_STALE_SECONDS) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        return (os.path.getmtime(path) + stale_after_seconds) >= time.time()
    except OSError:
        return False


def should_refresh_hextech(force: bool, stale_after_seconds: int = HIGH_FREQUENCY_STALE_SECONDS) -> bool:
    if force:
        return True
    latest_csv = get_latest_valid_csv()
    if latest_csv and hextech_refresh_blocked():
        return False
    return not _file_is_fresh(latest_csv or "", stale_after_seconds)


def is_first_run(force: bool = False) -> bool:
    if force:
        return True
    return not os.path.exists(CORE_DATA_FILE) or should_refresh_hextech(False) or should_refresh_synergy(False)


def _parse_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _synergy_refresh_blocked() -> bool:
    status = load_synergy_refresh_status()
    blocked_until = _parse_timestamp(status.get("blocked_until"))
    return status.get("last_result") == "blocked" and blocked_until > time.time()


def _write_synergy_refresh_status(result: str, reason: str = "") -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "last_attempt_at": now.isoformat(timespec="seconds"),
        "last_result": result,
        "reason": reason,
    }
    if result == "blocked":
        blocked_until = datetime.fromtimestamp(time.time() + SYNERGY_BLOCKED_COOLDOWN_SECONDS, tz=timezone.utc)
        payload["blocked_until"] = blocked_until.isoformat(timespec="seconds")
    atomic_write_json(build_synergy_refresh_status_path(), payload, ensure_ascii=False, indent=2)


def should_refresh_synergy(force: bool, stale_after_seconds: int = SYNERGY_STALE_SECONDS) -> bool:
    if not auto_synergy_refresh_enabled():
        return False
    if force:
        return True
    synergy_file = get_latest_synergy_snapshot_path()
    if not synergy_file or not os.path.exists(synergy_file):
        return True
    try:
        pointer_path = build_synergy_latest_pointer_path()
        meta = load_synergy_latest_pointer()
        healthy = (
            isinstance(meta, dict)
            and meta.get("version") == SYNERGY_REFRESH_META_VERSION
            and os.path.basename(str(synergy_file)) == str(meta.get("filename") or "")
            and int(meta.get("mapped") or 0) > 0
            and int(meta.get("non_empty_heroes") or 0) > 0
            and int(meta.get("synergy_entries") or 0) > 0
        )
        if not healthy:
            return True
        if _synergy_refresh_blocked():
            return False
        return not (_file_is_fresh(synergy_file, stale_after_seconds) and _file_is_fresh(pointer_path, stale_after_seconds))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True


def run_hero_sync() -> bool:
    return bool(sync_hero_data(allow_remote_check=True))


def run_hextech_refresh(stop_event=None, *, force: bool = False) -> bool:
    return bool(main_scraper(stop_event, force=force))


def run_synergy_refresh() -> bool:
    if not auto_synergy_refresh_enabled():
        _write_synergy_refresh_status("paused", "HEXTECH_AUTO_SYNERGY_REFRESH is not enabled")
        return False
    result = run_apex_spider()
    latest_path = get_latest_synergy_snapshot_path()
    if result and result.get("blocked"):
        _write_synergy_refresh_status("blocked", str(result.get("error") or "blocked"))
        return False
    if result and result.get("published") and latest_path and os.path.exists(latest_path):
        _write_synergy_refresh_status("success")
        return True
    return False


def run_augment_refresh(force_refresh: bool, stop_event=None) -> dict:
    return run_augment_icon_prefetch(
        force_refresh=force_refresh,
        stop_event=stop_event,
        max_workers=8,
    )


def current_api_cache_ready() -> bool:
    return bool(load_precomputed_champion_list()) and has_precomputed_hextech_cache()


def rebuild_api_cache_if_needed(force: bool = False) -> bool:
    latest_csv = get_latest_valid_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return current_api_cache_ready()
    if force or not current_api_cache_ready():
        return bool(rebuild_precomputed_api_cache_from_latest_csv())
    return True


def _run_mayhem_refresh_safely(stop_event=None) -> None:
    if stop_event is not None and stop_event.is_set():
        return
    from hextech.scraping.synergy.mayhem_refresh import run_mayhem_refresh

    try:
        run_mayhem_refresh(force=False)
    except Exception:
        logger.exception("Mayhem 低频刷新诊断写入失败")


def _result_from_report(report: dict, *, force: bool, correlation_id: str) -> RefreshResult:
    latest_valid_csv = get_latest_valid_csv() or ""
    latest_candidate_csv = get_latest_csv() or ""
    repaired = set(report.get("repaired", []) or [])
    fallback = set(report.get("fallback", []) or [])
    failed = set(report.get("failed", []) or [])
    requested = set(report.get("requested", []) or [])
    fallback_valid = bool(latest_valid_csv)
    remote_success = bool(repaired) and not fallback and not failed
    fallback_used = bool(fallback)

    if fallback_used and fallback_valid:
        state = "degraded"
        reason_code = "remote_failed_local_fallback"
        degradation_id = str(_ACTIVE_DEGRADATION.get("degradation_id") or _new_degradation_id())
    elif remote_success or (not requested and fallback_valid):
        state = "ready"
        reason_code = "refresh_success" if remote_success or force else "already_current"
        degradation_id = str(_ACTIVE_DEGRADATION.get("degradation_id") or "")
    else:
        state = "failed"
        reason_code = "refresh_failed_no_valid_fallback" if not fallback_valid else "refresh_failed"
        degradation_id = str(_ACTIVE_DEGRADATION.get("degradation_id") or _new_degradation_id())

    published_path = latest_valid_csv if fallback_valid else ""
    return RefreshResult(
        state=state,
        remote_success=remote_success,
        fallback_used=fallback_used,
        fallback_valid=fallback_valid,
        published=bool(published_path),
        published_data_path=published_path,
        data_version=_data_version_from_path(published_path or latest_candidate_csv),
        data_hash=_safe_file_hash(published_path),
        reason_code=reason_code,
        correlation_id=correlation_id,
        degradation_id=degradation_id,
        report=dict(report),
    )


def _ready_assertion_consistent(result: RefreshResult) -> bool:
    return result.state != "ready" or bool(result.published_data_path and result.fallback_valid)


def _write_refresh_state_event(result: RefreshResult) -> None:
    now = time.time()
    base_event = {
        "correlation_id": result.correlation_id,
        "degradation_id": result.degradation_id,
        "reason_code": result.reason_code,
        "error_type": "" if result.state == "ready" else result.reason_code,
        "error_message_sanitized": result.reason_code,
        "fallback_path": result.published_data_path if result.fallback_used else "",
        "fallback_version": result.data_version if result.fallback_used else "",
        "fallback_age_seconds": int(max(0.0, now - os.path.getmtime(result.published_data_path)))
        if result.fallback_used and result.published_data_path and os.path.exists(result.published_data_path)
        else 0,
        "fallback_validation": "valid" if result.fallback_valid else "invalid",
        "attempt_count": int(_ACTIVE_DEGRADATION.get("attempt_count") or 0),
        "ready_assertion_consistent": _ready_assertion_consistent(result),
        "published_data_path": result.published_data_path,
        "data_version": result.data_version,
        "data_hash": result.data_hash,
    }

    if result.state == "degraded":
        first_seen = float(_ACTIVE_DEGRADATION.get("first_seen") or now)
        is_reused = bool(_ACTIVE_DEGRADATION.get("degradation_id"))
        attempt_count = int(_ACTIVE_DEGRADATION.get("attempt_count") or 0) + 1
        _ACTIVE_DEGRADATION.update(
            {
                "degradation_id": result.degradation_id,
                "state": "degraded",
                "first_seen": first_seen,
                "last_seen": now,
                "attempt_count": attempt_count,
            }
        )
        base_event["attempt_count"] = attempt_count
        base_event["first_failed_at"] = datetime.fromtimestamp(first_seen, tz=timezone.utc).isoformat(timespec="seconds")
        base_event["last_failed_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        base_event["event"] = "fallback.reused" if is_reused else "fallback.activated"
        base_event["previous_state"] = "degraded" if is_reused else "ready"
        base_event["new_state"] = "degraded"
        _append_runtime_event(base_event)
        return

    if result.state == "ready" and _ACTIVE_DEGRADATION.get("degradation_id"):
        first_seen = float(_ACTIVE_DEGRADATION.get("first_seen") or now)
        previous_state = str(_ACTIVE_DEGRADATION.get("state") or "degraded")
        recovered_event = "refresh.recovered" if previous_state == "failed" else "fallback.recovered"
        event = dict(base_event)
        event.update(
            {
                "event": recovered_event,
                "previous_state": previous_state,
                "new_state": "ready",
                "level": "INFO",
                "degraded_duration_seconds": int(max(0.0, now - first_seen)),
                "recovered_data_path": result.published_data_path,
                "recovered_version": result.data_version,
                "recovered_hash": result.data_hash,
            }
        )
        _append_runtime_event(event)
        _ACTIVE_DEGRADATION.clear()
        return

    if result.state == "failed":
        previous_state = str(_ACTIVE_DEGRADATION.get("state") or "ready")
        if not _ACTIVE_DEGRADATION.get("degradation_id"):
            _ACTIVE_DEGRADATION.update(
                {
                    "degradation_id": result.degradation_id,
                    "state": "failed",
                    "first_seen": now,
                    "last_seen": now,
                    "attempt_count": 1,
                }
            )
        else:
            _ACTIVE_DEGRADATION["state"] = "failed"
            _ACTIVE_DEGRADATION["last_seen"] = now
            _ACTIVE_DEGRADATION["attempt_count"] = int(_ACTIVE_DEGRADATION.get("attempt_count") or 0) + 1
        base_event["event"] = "refresh.failed"
        base_event["level"] = "ERROR"
        base_event["previous_state"] = previous_state
        base_event["new_state"] = "failed"
        base_event["attempt_count"] = int(_ACTIVE_DEGRADATION.get("attempt_count") or 1)
        _append_runtime_event(base_event)


def refresh_backend_data(force: bool = False, stop_event=None) -> RefreshResult:
    """执行一次运行时自愈与后台刷新。

    返回结构化结果，显式区分 ready/degraded/failed，避免把 fallback 当作普通成功。
    """

    correlation_id = _new_correlation_id()
    report = heal_runtime_artifacts(force=force, stop_event=stop_event)
    _run_mayhem_refresh_safely(stop_event=stop_event)
    rebuild_api_cache_if_needed(force=force)
    result = _result_from_report(report, force=force, correlation_id=correlation_id)
    _write_refresh_state_event(result)
    if not _ready_assertion_consistent(result):
        _append_runtime_event(
            {
                "event": "state.assertion_mismatch",
                "level": "ERROR",
                "correlation_id": result.correlation_id,
                "degradation_id": result.degradation_id,
                "reason_code": "ready_validation_mismatch",
                "ready_assertion_consistent": False,
            }
        )
    return result


def heal_runtime_artifacts(force: bool = False, stop_event=None) -> dict:
    from hextech.scraping.heal_worker import heal_missing_artifacts

    return heal_missing_artifacts(force=force, stop_event=stop_event)


def get_startup_status_file() -> str:
    return build_runtime_state_path("startup_status.json")


__all__ = [
    "SYNERGY_FILE",
    "SYNERGY_BLOCKED_COOLDOWN_SECONDS",
    "SYNERGY_STALE_SECONDS",
    "auto_synergy_refresh_enabled",
    "current_api_cache_ready",
    "get_startup_status_file",
    "heal_runtime_artifacts",
    "is_augment_icon_prefetch_ready",
    "is_first_run",
    "manifest_has_incomplete_entries",
    "refresh_backend_data",
    "rebuild_api_cache_if_needed",
    "run_augment_refresh",
    "run_hero_sync",
    "run_hextech_refresh",
    "run_synergy_refresh",
    "should_refresh_hextech",
    "should_refresh_synergy",
]
