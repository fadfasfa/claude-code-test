"""Hextech 刷新状态、失败分类与公共请求支撑。"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from hextech.contracts import FailureKind, SourceHealth
from hextech.infrastructure.observability.logging import log_task_summary
from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult, fetch_text
from hextech.infrastructure.sources.hextech.parsing import HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso
from hextech.modules.data.source_runs import write_run_diagnostics
from hextech.modules.data.catalog.runtime_store import (
    build_runtime_state_path,
    get_latest_valid_csv,
    resolve_runtime_data_file,
)
from hextech.modules.data.ports.atomic import atomic_write_json

SCRAPER_BLOCKED_COOLDOWN_SECONDS = 30 * 60
SCRAPER_REMOTE_FAILURE_ESCALATION_THRESHOLD = 3
BLOCKED_HTTP_STATUS_CODES = {403, 429}
DEFERRED_REMOTE_FAILURE_REASONS = {"http_403", "http_429", "timeout"}
class RemoteFetchError(RuntimeError):
    """远端请求不可用；由调用方统一执行本地回退，避免逐英雄刷屏。"""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        url: str = "",
        error: str = "",
        context: str = "",
    ):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.url = url
        self.error = error
        self.context = context

def _scrapling_failure_reason(result: ScraplingFetchResult) -> tuple[str, int | None]:
    if result.status_code:
        return f"http_{result.status_code}", result.status_code

    if getattr(result, "error_kind", ""):
        return str(result.error_kind), None

    error_text = str(result.error or "").lower()
    if "curl: (35)" in error_text or "openssl_internal" in error_text or "tls connect" in error_text:
        return "tls_error", None
    if "timeout" in error_text or "timed out" in error_text:
        return "timeout", None
    if "connection" in error_text or "network" in error_text:
        return "network_error", None
    if result.error:
        return "scrapling_error", None
    return "empty_response", None


def _is_blocking_remote_failure(reason: str, status_code: int | None = None) -> bool:
    """只标记不应继续批量请求的明确限流/拒绝响应。

    单请求 timeout 仍属于最终失败后的 cooldown 分类，但首轮详情 timeout
    必须先进入低并发 tail retry，不能在已有重试分支前提前返回。
    """

    if status_code in BLOCKED_HTTP_STATUS_CODES:
        return True
    return reason in {"http_403", "http_429"}


def _is_deferred_remote_failure(reason: str, status_code: int | None = None) -> bool:
    if status_code in BLOCKED_HTTP_STATUS_CODES:
        return True
    return reason in DEFERRED_REMOTE_FAILURE_REASONS


def _remote_failure_count(previous: dict, reason: str) -> int:
    if not _is_deferred_remote_failure(reason):
        return 0
    # 403/429/timeout 都属于同一类远端阻断；reason 变化也应累计连续失败。
    if not _is_deferred_remote_failure(str(previous.get("reason") or "")):
        return 1
    try:
        return int(previous.get("consecutive_remote_failures") or 0) + 1
    except (TypeError, ValueError):
        return 1


def _summarize_detail_failures(failures: list[dict], *, label: str) -> None:
    if not failures:
        return
    reasons = Counter(str(item.get("reason") or "unknown") for item in failures)
    samples = []
    for item in failures[:5]:
        name = str(item.get("name") or item.get("champ", {}).get("championId") or "?")
        reason = str(item.get("reason") or "unknown")
        url = str(item.get("url") or "")
        samples.append(f"{name}:{reason}:{url}")
    logging.warning(
        "海克斯详情失败摘要：label=%s count=%s reasons=%s samples=%s",
        label,
        len(failures),
        dict(reasons),
        "; ".join(samples),
    )


def fetch_with_retry(
    url,
    max_retries=1,
    timeout=6,
    *,
    quiet: bool = False,
    raise_on_failure: bool = False,
    caller: str = "hextech",
    context: str = "",
):
    # Scrapling 接管远端 HTTP 获取；业务层仍用旧 retry/fallback 契约。
    for attempt in range(max_retries):
        result = fetch_text(
            url,
            timeout_ms=int(timeout * 1000),
            caller=caller,
            max_attempts=2,
        )
        if not result.error and result.status_code and 200 <= result.status_code < 400:
            return result

        reason, status_code = _scrapling_failure_reason(result)
        if attempt < max_retries - 1:
            wait_time = 2 ** (attempt + 1)
            if not quiet:
                logging.warning(
                    "请求失败后重试：caller=%s context=%s url=%s attempt=%s/%s reason=%s status=%s wait=%ss",
                    caller,
                    context,
                    url,
                    attempt + 1,
                    max_retries,
                    reason,
                    status_code,
                    wait_time,
                )
            time.sleep(wait_time)
        else:
            if raise_on_failure:
                raise RemoteFetchError(
                    reason,
                    status_code=status_code,
                    url=url,
                    error=result.error,
                    context=context,
                )
            if not quiet:
                logging.warning(
                    "请求失败：caller=%s context=%s url=%s attempts=%s reason=%s status=%s error=%s",
                    caller,
                    context,
                    url,
                    getattr(result, "attempts", 1),
                    reason,
                    status_code,
                    result.error,
                )
    return None


def load_scraper_status() -> dict:
    """兼容只含 last_success_time 的旧状态文件。"""

    status_file = resolve_runtime_data_file(
        build_runtime_state_path("scraper_status.json"),
    )
    if not status_file or not os.path.exists(status_file):
        return {}
    try:
        with open(status_file, "r", encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _timestamp_from_status(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def hextech_refresh_blocked(status: dict | None = None) -> bool:
    payload = status if isinstance(status, dict) else load_scraper_status()
    return _timestamp_from_status(payload.get("blocked_until")) > time.time()


def _new_attempt_context() -> dict:
    """记录一次刷新尝试的诊断字段；只保存统计和样本，不保存响应正文。"""

    started = time.time()
    return {
        "attempt_id": f"hextech-{datetime.fromtimestamp(started, tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(timespec="seconds"),
        "ended_at": "",
        "duration_seconds": 0.0,
        "result": "running",
        "failure_stage": "",
        "reason": "",
        "total_heroes": 0,
        "completed_heroes": 0,
        "cdn_hit_count": 0,
        "slow_path_count": 0,
        "success_rows": 0,
        "failure_count": 0,
        "failure_samples": [],
        "active_csv": "",
        "fallback_used": False,
        "output_csv": "",
    }


def _finish_attempt(
    attempt: dict | None,
    *,
    result: str,
    reason: str = "",
    failure_stage: str = "",
    active_csv: str = "",
    output_csv: str = "",
    fallback_used: bool = False,
) -> dict:
    if not isinstance(attempt, dict):
        attempt = _new_attempt_context()
    ended = time.time()
    started_at = _timestamp_from_status(attempt.get("started_at")) or ended
    attempt.update(
        {
            "ended_at": datetime.fromtimestamp(ended, tz=timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": round(max(0.0, ended - started_at), 3),
            "result": result,
            "reason": reason,
            "failure_stage": failure_stage,
            "active_csv": active_csv,
            "fallback_used": bool(fallback_used),
            "output_csv": output_csv,
        }
    )
    return attempt


def _detail_source_from_url(url: str) -> str:
    text = str(url or "")
    if text.startswith(HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL):
        return "cdn"
    return "slow" if text else "unknown"


def _failure_sample(item: dict) -> dict:
    champ = item.get("champ") if isinstance(item.get("champ"), dict) else {}
    return {
        "champion_id": str(champ.get("championId") or ""),
        "name": str(item.get("name") or ""),
        "reason": str(item.get("reason") or ""),
        "status_code": item.get("status_code"),
        "source": _detail_source_from_url(str(item.get("url") or "")),
        "error": str(item.get("error") or "")[:200],
    }


def _record_detail_result(attempt: dict, item: dict) -> None:
    source = _detail_source_from_url(str(item.get("url") or ""))
    if item.get("rows"):
        attempt["success_rows"] = int(attempt.get("success_rows") or 0) + len(item["rows"])
        if source == "cdn":
            attempt["cdn_hit_count"] = int(attempt.get("cdn_hit_count") or 0) + 1
        elif source == "slow":
            attempt["slow_path_count"] = int(attempt.get("slow_path_count") or 0) + 1
    else:
        attempt["failure_count"] = int(attempt.get("failure_count") or 0) + 1
        if source == "slow":
            attempt["slow_path_count"] = int(attempt.get("slow_path_count") or 0) + 1
        samples = list(attempt.get("failure_samples") or [])
        if len(samples) < 8:
            samples.append(_failure_sample(item))
        attempt["failure_samples"] = samples


def _write_scraper_status(
    result: str,
    reason: str = "",
    *,
    active_csv: str = "",
    attempt: dict | None = None,
) -> dict:
    now = time.time()
    previous = load_scraper_status()
    remote_failure_count = _remote_failure_count(previous, reason) if result in {"fallback", "failed"} else 0
    blocked_until = ""
    if remote_failure_count:
        blocked_until = datetime.fromtimestamp(now + SCRAPER_BLOCKED_COOLDOWN_SECONDS, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    last_attempt = dict(attempt) if isinstance(attempt, dict) else dict(previous.get("last_attempt") or {})
    payload = dict(previous)
    payload.update(
        {
            "last_attempt_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
            "last_result": result,
            "reason": reason,
            "blocked_until": blocked_until,
            "next_retry_at": blocked_until,
            "consecutive_remote_failures": remote_failure_count,
            "remote_failure_escalated": remote_failure_count >= SCRAPER_REMOTE_FAILURE_ESCALATION_THRESHOLD,
            "bypass_evaluation_hint": (
                "连续远端失败达到阈值；保留 last-good 并降低后续请求频率。"
                if remote_failure_count >= SCRAPER_REMOTE_FAILURE_ESCALATION_THRESHOLD
                else ""
            ),
            "active_csv": active_csv,
            "active_csv_mtime": os.path.getmtime(active_csv) if active_csv and os.path.exists(active_csv) else 0.0,
            "last_success_time": previous.get("last_success_time", 0),
            "last_attempt": last_attempt,
            "last_attempt_id": last_attempt.get("attempt_id", ""),
            "failure_stage": last_attempt.get("failure_stage", ""),
            "cdn_hit_count": last_attempt.get("cdn_hit_count", 0),
            "slow_path_count": last_attempt.get("slow_path_count", 0),
            "success_rows": last_attempt.get("success_rows", 0),
            "failure_samples": last_attempt.get("failure_samples", []),
            "fallback_used": bool(active_csv and result == "fallback"),
        }
    )
    if result == "success":
        payload["last_success_time"] = now
        payload["consecutive_remote_failures"] = 0
        payload["remote_failure_escalated"] = False
        payload["bypass_evaluation_hint"] = ""
        payload["next_retry_at"] = ""
    atomic_write_json(build_runtime_state_path("scraper_status.json"), payload, ensure_ascii=False, indent=2)
    return payload


def _finish_refresh_failure(
    reason: str,
    *,
    started_at: float,
    attempt: dict | None = None,
    failure_stage: str = "",
) -> bool:
    active_csv = get_latest_valid_csv() or ""
    if isinstance(attempt, dict) and attempt.get("attempt_id"):
        reason_kind = str(reason or "")
        try:
            failure_kind = FailureKind(reason_kind)
        except ValueError:
            failure_kind = FailureKind.SCHEMA_CHANGED if "parse" in reason_kind or "schema" in reason_kind else FailureKind.INVALID_PAYLOAD
        samples = list(attempt.get("failure_samples") or [])
        outcomes = tuple(
            ItemOutcome(
                item_id=str(sample.get("champion_id") or ""),
                state="failed",
                stage=failure_stage or "unknown",
                failure_kind=failure_kind,
                details=dict(sample),
            )
            for sample in samples
        )
        manifest = SourceRunManifest(
            source="hextech",
            run_id=str(attempt["attempt_id"]),
            health=SourceHealth.FAILED,
            started_at=str(attempt.get("started_at") or utc_now_iso()),
            finished_at=utc_now_iso(),
            expected_items=int(attempt.get("total_heroes") or 0),
            successful_items=max(0, int(attempt.get("completed_heroes") or 0) - int(attempt.get("failure_count") or 0)),
            confirmed_empty_items=0,
            failed_items=max(1, int(attempt.get("failure_count") or 0)),
            record_count=int(attempt.get("success_rows") or 0),
            artifact="stats.csv",
            outcomes=outcomes,
            metadata={"failure_stage": failure_stage, "reason": reason},
        )
        write_run_diagnostics(manifest, report={"failure_samples": samples})
    if active_csv:
        attempt = _finish_attempt(
            attempt,
            result="fallback",
            reason=reason,
            failure_stage=failure_stage or reason,
            active_csv=active_csv,
            fallback_used=True,
        )
        status = _write_scraper_status("fallback", reason, active_csv=active_csv, attempt=attempt)
        logging.warning(
            "海克斯远端刷新失败：reason=%s active_csv=%s next_retry_at=%s consecutive_remote_failures=%s escalated=%s",
            reason,
            os.path.basename(active_csv),
            status.get("next_retry_at") or "",
            status.get("consecutive_remote_failures") or 0,
            status.get("remote_failure_escalated") or False,
        )
        return True
    attempt = _finish_attempt(
        attempt,
        result="failed",
        reason=reason,
        failure_stage=failure_stage or reason,
        active_csv="",
        fallback_used=False,
    )
    status = _write_scraper_status("failed", reason, active_csv="", attempt=attempt)
    logging.warning(
        "海克斯远端刷新失败且无本地 CSV：reason=%s next_retry_at=%s consecutive_remote_failures=%s escalated=%s",
        reason,
        status.get("next_retry_at") or "",
        status.get("consecutive_remote_failures") or 0,
        status.get("remote_failure_escalated") or False,
    )
    log_task_summary(
        logging.getLogger(__name__),
        task="海克斯抓取",
        started_at=started_at,
        success=False,
        detail=f"error={reason}; no_valid_local_csv",
    )
    return False


def check_execution_permission(force: bool = False):
    if force:
        return True, "手动强制刷新，忽略冷却与新鲜度检查..."
    status = load_scraper_status()
    if hextech_refresh_blocked(status) and get_latest_valid_csv():
        return False, "远端处于 30 分钟冷却期，继续使用本地有效 CSV，到期后自动重抓。"
    status_file = resolve_runtime_data_file(
        build_runtime_state_path("scraper_status.json"),
    )
    now = time.time()
    current_csv = get_latest_valid_csv()
    if not current_csv:
        return True, "当前没有完整 Hextech 来源，启动抓取..."
    if not status_file or not os.path.exists(status_file):
        return True, "首次运行，启动抓取..."
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            last_run = json.load(f).get("last_success_time", 0)
            if datetime.fromtimestamp(now).date() > datetime.fromtimestamp(last_run).date():
                return True, "跨天自动同步..."
            if (now - last_run) / 3600 >= 4:
                return True, "数据过时，执行同步..."
            return False, "数据尚在有效期内，跳过抓取。"
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return True, "状态文件异常，强制刷新..."

def update_status_file(active_csv: str = "", *, attempt: dict | None = None):
    """记录已通过来源门禁的成功状态。"""

    attempt = _finish_attempt(
        attempt,
        result="success",
        reason="",
        active_csv=active_csv,
        output_csv=active_csv,
        fallback_used=False,
    )
    return _write_scraper_status("success", active_csv=active_csv, attempt=attempt)
