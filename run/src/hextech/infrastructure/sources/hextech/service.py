"""英雄海克斯排名抓取器。

调用方: DataService acquisition、抓取门禁；关键依赖: runtime_store、BeautifulSoup、pandas。
"""

import json
import time
import pandas as pd
import os
import logging
from hextech.modules.data.catalog.runtime_store import (
    get_latest_valid_csv,
)
from hextech.infrastructure.sources.version_sync import (
    HEXTECH_AUGMENT_METADATA_URLS,
    HEXTECH_CHAMPION_STATS_URLS,
)
from hextech.modules.data.catalog.version_catalog import load_augment_tier_map, load_champion_core_data
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.infrastructure.sources.hextech.detail_runner import DETAIL_PASS_RUNNER
from hextech.infrastructure.sources.hextech.detail_fetch import (
    fetch_champion_detail_stats_fast,
)
from hextech.infrastructure.sources.hextech.publisher import publish_hextech_run
from hextech.infrastructure.sources.hextech.source import ChampionCatalogMismatch, build_expected_champions
from hextech.modules.acquisition.common.contracts import ItemOutcome
from hextech.modules.acquisition.hextech.coverage import HextechCoverageError
from hextech.infrastructure.observability.logging import log_task_summary

HEXTECH_DETAIL_WORKERS = 4
HEXTECH_DETAIL_RETRY_WORKERS = 2
HEXTECH_DETAIL_TIMEOUT_SECONDS = 6
HEXTECH_DETAIL_RETRY_TIMEOUT_SECONDS = 12
HEXTECH_DETAIL_POOL_TIMEOUT_SECONDS = 180
HEXTECH_DETAIL_RETRY_POOL_TIMEOUT_SECONDS = 120
HEXTECH_HANDSHAKE_RETRIES = 2
HEXTECH_HANDSHAKE_TIMEOUT_SECONDS = 6
from hextech.infrastructure.sources.hextech.parsing import _clean_augment_text, _metadata_tier_from_rarity  # noqa: E402
from hextech.infrastructure.sources.hextech.refresh_support import (  # noqa: E402
    RemoteFetchError,
    _finish_refresh_failure,
    _is_blocking_remote_failure,
    _new_attempt_context,
    _record_detail_result,
    _summarize_detail_failures,
    check_execution_permission,
    fetch_with_retry,
    update_status_file,
)



def _upstream_metadata_summary(response: object, payload: object) -> tuple[str, str]:
    """提取上游版本/日期用于覆盖诊断；缺失时留空而不是虚构版本。"""

    mapping = payload if isinstance(payload, dict) else {}
    nested = mapping.get("meta") if isinstance(mapping.get("meta"), dict) else {}
    version = next(
        (
            str(value).strip()
            for value in (
                mapping.get("version"),
                mapping.get("patch"),
                mapping.get("dataVersion"),
                nested.get("version"),
            )
            if str(value or "").strip()
        ),
        "",
    )
    headers = getattr(response, "headers", {})
    header_date = headers.get("Last-Modified") if hasattr(headers, "get") else ""
    date = next(
        (
            str(value).strip()
            for value in (
                mapping.get("updated_at"),
                mapping.get("updatedAt"),
                mapping.get("date"),
                nested.get("updated_at"),
                header_date,
            )
            if str(value or "").strip()
        ),
        "",
    )
    return version, date


def _metadata_stat_ids(payload: object) -> list[str]:
    """只提取实际海克斯条目，排除 metadata 和版本对象。"""

    mapping = payload if isinstance(payload, dict) else {}
    identifiers: list[str] = []
    for raw_key, raw_item in mapping.items():
        if not isinstance(raw_item, dict):
            continue
        key = str(raw_key or "").strip()
        if not key:
            continue
        has_identity = any(
            str(raw_item.get(field) or "").strip()
            for field in ("displayName", "name", "augmentName", "id", "augmentId")
        )
        if has_identity:
            identifiers.append(key)
    return identifiers


def probe_hextech_upstream_marker() -> dict[str, str]:
    """读取既有 metadata 端点的轻量版本标记，不创建独立调度任务。"""

    for url in HEXTECH_AUGMENT_METADATA_URLS:
        try:
            response = fetch_with_retry(
                url,
                max_retries=1,
                timeout=HEXTECH_HANDSHAKE_TIMEOUT_SECONDS,
                quiet=True,
                raise_on_failure=True,
                caller="hextech_marker_probe",
                context="augment_metadata",
            )
            if response is None:
                continue
            payload = response.json()
            if isinstance(payload, dict):
                version, date = _upstream_metadata_summary(response, payload)
                if version or date:
                    return {"version": version, "date": date}
        except (RemoteFetchError, ValueError, TypeError):
            continue
    return {}

def _main_scraper_impl(
    stop_event=None,
    force: bool = False,
    *,
    promote_current: bool = False,
    pointer_output: str | os.PathLike[str] | None = None,
    _started_at: float | None = None,
    _attempt: dict | None = None,
):
    started_at = time.time() if _started_at is None else _started_at
    attempt = _new_attempt_context() if _attempt is None else _attempt
    output_csv = ""
    attempt["output_csv"] = output_csv

    can_run, msg = check_execution_permission(force=force)
    if not can_run:
        logging.info("海克斯抓取跳过：%s", msg)
        return bool(get_latest_valid_csv())

    logging.info("海克斯抓取开始：%s", msg)
    catalog = load_active_catalog()
    truth_dict = load_augment_tier_map(catalog.root)
    core_data = load_champion_core_data(catalog.root)
    if not truth_dict or not core_data:
        return _finish_refresh_failure(
            "base_data_missing",
            started_at=started_at,
            attempt=attempt,
            failure_stage="base_data",
        )

    try:
        aug_response = None
        aug_error = ""
        for url in HEXTECH_AUGMENT_METADATA_URLS:
            try:
                candidate = fetch_with_retry(
                    url,
                    max_retries=HEXTECH_HANDSHAKE_RETRIES,
                    timeout=HEXTECH_HANDSHAKE_TIMEOUT_SECONDS,
                    quiet=True,
                    raise_on_failure=True,
                    caller="hextech_metadata",
                    context="augment_metadata",
                )
            except RemoteFetchError as e:
                aug_error = e.reason
                continue
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), dict):
                    aug_response = candidate
                    break
            except Exception:
                aug_error = "metadata_invalid"
                continue
        if aug_response is None:
            return _finish_refresh_failure(
                aug_error or "metadata_invalid",
                started_at=started_at,
                attempt=attempt,
                failure_stage="augment_metadata",
            )
        aug_data = aug_response.json()
        metadata_ids = _metadata_stat_ids(aug_data)
        upstream_version, upstream_date = _upstream_metadata_summary(aug_response, aug_data)

        aug_id_map = {}
        aug_tier_map = {}
        for raw_key, raw_item in aug_data.items():
            item = raw_item if isinstance(raw_item, dict) else {}
            aug_id = str(raw_key)
            display_name = _clean_augment_text(item.get('displayName'))
            aug_id_map[aug_id] = display_name
            aug_tier_map[aug_id] = truth_dict.get(display_name) or _metadata_tier_from_rarity(item.get("rarity"))

        stats_response = None
        stats_error = ""
        for url in HEXTECH_CHAMPION_STATS_URLS:
            try:
                candidate = fetch_with_retry(
                    url,
                    max_retries=HEXTECH_HANDSHAKE_RETRIES,
                    timeout=HEXTECH_HANDSHAKE_TIMEOUT_SECONDS,
                    quiet=True,
                    raise_on_failure=True,
                    caller="hextech_champion_stats",
                    context="champions-stats",
                )
            except RemoteFetchError as e:
                stats_error = e.reason
                continue
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), list):
                    stats_response = candidate
                    break
            except Exception:
                stats_error = "stats_invalid"
                continue
        if stats_response is None:
            return _finish_refresh_failure(
                stats_error or "stats_invalid",
                started_at=started_at,
                attempt=attempt,
                failure_stage="champion_stats",
            )
        stats_list = stats_response.json()
        if not stats_list:
            return _finish_refresh_failure(
                "stats_empty",
                started_at=started_at,
                attempt=attempt,
                failure_stage="champion_stats",
            )
        try:
            stats_list = build_expected_champions(core_data, stats_list)
        except ChampionCatalogMismatch:
            return _finish_refresh_failure(
                "schema_changed",
                started_at=started_at,
                attempt=attempt,
                failure_stage="champion_catalog",
            )
    except RemoteFetchError as e:
        return _finish_refresh_failure(
            e.reason,
            started_at=started_at,
            attempt=attempt,
            failure_stage=e.context or "handshake",
        )
    except (ValueError, json.JSONDecodeError):
        return _finish_refresh_failure(
            "handshake_invalid",
            started_at=started_at,
            attempt=attempt,
            failure_stage="handshake",
        )

    attempt["total_heroes"] = len(stats_list)
    # 先声明预检状态，避免 worker 闭包依赖后续才创建的局部变量。
    preflight_id = ""
    preflight_result: dict = {}
    preflight_rows: list = []

    def fetch_champ_detail(champ: dict, *, timeout: int, preflight_rows: list | None = None) -> dict:
        if preflight_rows is not None:
            c_id = str(champ.get('championId', ''))
            c_name = core_data.get(c_id, {}).get("name", c_id)
            return {
                "champ": champ,
                "name": c_name,
                "rows": list(preflight_rows),
                "reason": "",
                "status_code": preflight_result.get("status_code"),
                "url": preflight_result.get("url", ""),
                "error": preflight_result.get("error", ""),
            }
        return fetch_champion_detail_stats_fast(
            champ,
            core_data=core_data,
            aug_id_map=aug_id_map,
            truth_dict=truth_dict,
            aug_tier_map=aug_tier_map,
            timeout=timeout,
        )

    def run_detail_pass(champs: list[dict], *, workers: int, timeout: int, pool_timeout: int, label: str):
        pass_rows = []
        failures = []
        logging.info(
            "Hextech detail pass: label=%s heroes=%s workers=%s request_timeout=%s pool_timeout=%s",
            label,
            len(champs),
            workers,
            timeout,
            pool_timeout,
        )
        def worker(champ: dict) -> dict:
            c_id = str(champ.get("championId", ""))
            cached_rows = preflight_rows if c_id == preflight_id else None
            return fetch_champ_detail(champ, timeout=timeout, preflight_rows=cached_rows)

        outcome = DETAIL_PASS_RUNNER.run(
            champs,
            worker=worker,
            max_workers=workers,
            timeout_seconds=pool_timeout,
            stop_event=stop_event,
        )
        if outcome.status == "draining":
            logging.warning("Hextech detail pass skipped while previous workers drain: label=%s", label)
            return pass_rows, failures, "detail_pass_draining"

        for champ, item in outcome.results:
            if item["rows"]:
                pass_rows.extend(item["rows"])
            else:
                failures.append(item)
            attempt["completed_heroes"] = int(attempt.get("completed_heroes") or 0) + 1
            _record_detail_result(attempt, item)

        for champ, error in outcome.errors:
            logging.error("Worker result collection failed: %s", error)
            failure_item = {
                "champ": champ,
                "name": "",
                "rows": [],
                "reason": "worker_error",
                "status_code": None,
                "url": "",
                "error": str(error),
            }
            failures.append(failure_item)
            attempt["completed_heroes"] = int(attempt.get("completed_heroes") or 0) + 1
            _record_detail_result(attempt, failure_item)

        if outcome.status in {"timed_out", "stopped"}:
            reason = "thread_pool_timeout" if outcome.status == "timed_out" else "stopped"
            logging.error(
                "Hextech detail pass ended early: label=%s reason=%s pending=%s",
                label,
                reason,
                len(outcome.pending_items),
            )
            for champ in outcome.pending_items:
                c_id = str(champ.get("championId", ""))
                failure_item = {
                    "champ": champ,
                    "name": core_data.get(c_id, {}).get("name", c_id),
                    "rows": [],
                    "reason": reason,
                    "status_code": None,
                    "url": "",
                    "error": f"detail pass ended early: {label}",
                }
                failures.append(failure_item)
                attempt["completed_heroes"] = int(attempt.get("completed_heroes") or 0) + 1
                _record_detail_result(attempt, failure_item)
            return pass_rows, failures, reason
        return pass_rows, failures, ""

    # 先请求一个英雄详情。源站封禁时在创建 worker 线程池前熔断；瞬时超时做一次更长预检。
    preflight_champ = stats_list[0]
    preflight_id = str(preflight_champ.get("championId", ""))
    preflight_result = fetch_champ_detail(preflight_champ, timeout=HEXTECH_DETAIL_TIMEOUT_SECONDS)
    if not preflight_result["rows"] and not _is_blocking_remote_failure(
        preflight_result["reason"],
        preflight_result["status_code"],
    ):
        preflight_result = fetch_champ_detail(preflight_champ, timeout=HEXTECH_DETAIL_RETRY_TIMEOUT_SECONDS)
    if not preflight_result["rows"]:
        reason = preflight_result["reason"] or "preflight_empty"
        if reason == "parse_error":
            reason = "preflight_parse_error"
        elif reason == "no_valid_rows":
            reason = "preflight_no_valid_rows"
        _record_detail_result(attempt, preflight_result)
        return _finish_refresh_failure(
            reason,
            started_at=started_at,
            attempt=attempt,
            failure_stage="detail_preflight",
        )
    preflight_rows = preflight_result["rows"]

    all_rows, detail_failures, pass_error = run_detail_pass(
        list(stats_list),
        workers=HEXTECH_DETAIL_WORKERS,
        timeout=HEXTECH_DETAIL_TIMEOUT_SECONDS,
        pool_timeout=HEXTECH_DETAIL_POOL_TIMEOUT_SECONDS,
        label="initial",
        )
    if pass_error:
        return _finish_refresh_failure(
            pass_error,
            started_at=started_at,
            attempt=attempt,
            failure_stage="detail_initial",
        )
    _summarize_detail_failures(detail_failures, label="initial")

    blocking_failures = [
        item for item in detail_failures if _is_blocking_remote_failure(item["reason"], item["status_code"])
    ]
    if blocking_failures:
        return _finish_refresh_failure(
            blocking_failures[0]["reason"],
            started_at=started_at,
            attempt=attempt,
            failure_stage="detail_initial",
        )

    if detail_failures:
        retry_champs = [item["champ"] for item in detail_failures if item.get("champ")]
        logging.warning("海克斯详情首轮失败 %s 个，进入低并发尾部重试。", len(retry_champs))
        retry_rows, retry_failures, retry_error = run_detail_pass(
            retry_champs,
            workers=HEXTECH_DETAIL_RETRY_WORKERS,
            timeout=HEXTECH_DETAIL_RETRY_TIMEOUT_SECONDS,
            pool_timeout=HEXTECH_DETAIL_RETRY_POOL_TIMEOUT_SECONDS,
            label="tail-retry",
        )
        all_rows.extend(retry_rows)
        if retry_error:
            return _finish_refresh_failure(
                retry_error,
                started_at=started_at,
                attempt=attempt,
                failure_stage="detail_retry",
            )
        _summarize_detail_failures(retry_failures, label="tail-retry")
        blocking_retry_failures = [
            item for item in retry_failures if _is_blocking_remote_failure(item["reason"], item["status_code"])
        ]
        if blocking_retry_failures:
            return _finish_refresh_failure(
                blocking_retry_failures[0]["reason"],
                started_at=started_at,
                attempt=attempt,
                failure_stage="detail_retry",
            )
        if retry_failures:
            failed_names = ", ".join(str(item["name"]) for item in retry_failures[:5])
            logging.warning("海克斯详情尾部重试后仍失败 %s 个：%s", len(retry_failures), failed_names)
            return _finish_refresh_failure(
                f"detail_failed_{len(retry_failures)}",
                started_at=started_at,
                attempt=attempt,
                failure_stage="detail_retry",
            )

    if all_rows:
        df = pd.DataFrame(all_rows)
        df['胜率差'] = df['海克斯胜率'] - df['英雄胜率']

        wr_std = df['胜率差'].std()
        pr_std = df['海克斯出场率'].std()
        if wr_std == 0:
            wr_std = 1
        if pr_std == 0:
            pr_std = 1

        z_wr = (df['胜率差'] - df['胜率差'].mean()) / wr_std
        z_pr = (df['海克斯出场率'] - df['海克斯出场率'].mean()) / pr_std

        # 胜率差为正时增加出场率加成，为负时则扣减
        sign_mask = df['胜率差'].apply(lambda x: 1 if x >= 0 else -1)
        df['综合得分'] = z_wr * 0.85 + z_pr * 0.15 * sign_mask

        if '源站排名' in df.columns:
            df.sort_values(
                by=['英雄名称', '源站排名'],
                ascending=[True, True],
                inplace=True
            )
        else:
            df.sort_values(
                by=['英雄名称', '海克斯阶级', '综合得分'],
                ascending=[True, True, False],
                inplace=True
            )

        # 数据量过低时直接拒绝覆盖结果
        if len(df) < 300:
            return _finish_refresh_failure(
                f"insufficient_rows_{len(df)}",
                started_at=started_at,
                attempt=attempt,
                failure_stage="publish_validation",
            )

        expected_ids = [str(item.get("championId") or "") for item in stats_list]
        outcomes = tuple(
            ItemOutcome(
                item_id=champion_id,
                state="success",
                stage="detail",
                record_count=int((df["英雄ID"].astype(str).str.replace(".0", "", regex=False) == champion_id).sum()),
            )
            for champion_id in expected_ids
        )
        try:
            output_csv, _ = publish_hextech_run(
                df,
                run_id=str(attempt["attempt_id"]),
                expected_hero_ids=expected_ids,
                outcomes=outcomes,
                started_at=str(attempt["started_at"]),
                metadata_ids=metadata_ids,
                upstream_version=upstream_version,
                upstream_date=upstream_date,
                promote_current=promote_current,
                pointer_output=pointer_output,
            )
        except HextechCoverageError as exc:
            # 候选代虽不能发布，覆盖报告仍是排查“为何仍显示 last-good”的核心证据。
            attempt["coverage"] = dict(exc.coverage or {})
            logging.error("Hextech 来源覆盖门禁失败：%s", exc)
            return _finish_refresh_failure(
                "coverage_gate_failed",
                started_at=started_at,
                attempt=attempt,
                failure_stage="coverage_validation",
            )
        except ValueError as exc:
            logging.error("Hextech 来源发布门禁失败：%s", exc)
            return _finish_refresh_failure(
                "schema_changed",
                started_at=started_at,
                attempt=attempt,
                failure_stage="publish_validation",
            )

        attempt["success_rows"] = len(df)
        update_status_file(output_csv, attempt=attempt)
        log_task_summary(
            logging.getLogger(__name__),
            task="海克斯抓取",
            started_at=started_at,
            success=True,
            detail=f"rows={len(df)} output={os.path.basename(output_csv)}",
        )
        return True
    else:
        return _finish_refresh_failure(
            "no_valid_rows",
            started_at=started_at,
            attempt=attempt,
            failure_stage="detail_rows",
        )


def main_scraper(
    stop_event=None,
    force: bool = False,
    *,
    promote_current: bool = False,
    pointer_output: str | os.PathLike[str] | None = None,
):
    """执行完整 Hextech 抓取；未预期异常必须留下失败诊断后继续向上抛出。"""

    started_at = time.time()
    attempt = _new_attempt_context()
    try:
        return _main_scraper_impl(
            stop_event,
            force,
            promote_current=promote_current,
            pointer_output=pointer_output,
            _started_at=started_at,
            _attempt=attempt,
        )
    except Exception as exc:
        attempt["failure_count"] = max(1, int(attempt.get("failure_count") or 0))
        samples = list(attempt.get("failure_samples") or [])
        samples.append(
            {
                "champion_id": "",
                "stage": "unexpected_exception",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:1000],
            }
        )
        attempt["failure_samples"] = samples[-20:]
        try:
            _finish_refresh_failure(
                "unexpected_exception",
                started_at=started_at,
                attempt=attempt,
                failure_stage="unexpected_exception",
            )
        except Exception:
            logging.exception("Hextech 未预期异常的失败诊断写入失败")
        logging.exception("Hextech 抓取发生未预期异常")
        raise
