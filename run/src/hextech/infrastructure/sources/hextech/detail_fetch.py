"""Hextech 单英雄详情的静态 CDN/HTML 快链路。

只复用现有公开静态端点；不使用 browser、stealth、登录态或后台任务补齐统计。
"""

from __future__ import annotations

import json
import logging

from hextech.infrastructure.sources.hextech.parsing import (
    build_hextech_champion_detail_json_url,
    extract_champion_detail_json_stats,
    extract_champion_stats,
)
from hextech.infrastructure.sources.hextech.refresh_support import (
    RemoteFetchError,
    _is_deferred_remote_failure,
    fetch_with_retry,
)
from hextech.infrastructure.sources.version_sync import build_hextech_detail_urls


def fetch_champion_detail_stats_fast(
    champ: dict,
    *,
    core_data: dict,
    aug_id_map: dict,
    truth_dict: dict,
    aug_tier_map: dict | None,
    timeout: int,
) -> dict:
    """优先使用 CDN JSON，必要时回退到同源静态 HTML。"""

    c_id = str(champ.get("championId", ""))
    c_name = core_data.get(c_id, {}).get("name", c_id)
    last_reason = "empty_response"
    last_status_code = None
    last_url = ""
    last_error = ""

    detail_json_url = build_hextech_champion_detail_json_url(c_id)
    last_url = detail_json_url
    try:
        res = fetch_with_retry(
            detail_json_url,
            timeout=timeout,
            quiet=True,
            raise_on_failure=True,
            caller="hextech_detail_json",
            context=f"championId={c_id};champion={c_name}",
        )
    except RemoteFetchError as error:
        last_reason = error.reason
        last_status_code = error.status_code
        last_url = error.url or detail_json_url
        last_error = error.error
        if _is_deferred_remote_failure(error.reason, error.status_code):
            return {
                "champ": champ,
                "name": c_name,
                "rows": [],
                "reason": last_reason,
                "status_code": last_status_code,
                "url": last_url,
                "error": last_error,
            }
    else:
        if res is not None and res.status_code == 200 and res.text:
            try:
                rows = extract_champion_detail_json_stats(
                    res.json(), aug_id_map, truth_dict, c_id, c_name, champ, aug_tier_map
                )
                if rows:
                    logging.info("[%s] CDN JSON 快链路命中：championId=%s rows=%s", c_name, c_id, len(rows))
                    return {
                        "champ": champ,
                        "name": c_name,
                        "rows": rows,
                        "reason": "",
                        "status_code": res.status_code,
                        "url": detail_json_url,
                        "error": "",
                    }
                last_reason = "detail_json_no_valid_rows"
            except (TypeError, ValueError, json.JSONDecodeError):
                rows = extract_champion_stats(res.text, aug_id_map, truth_dict, c_id, c_name, champ, aug_tier_map)
                if rows:
                    logging.warning("[%s] CDN JSON 非 JSON 但页面解析可用：championId=%s rows=%s", c_name, c_id, len(rows))
                    return {
                        "champ": champ,
                        "name": c_name,
                        "rows": rows,
                        "reason": "",
                        "status_code": res.status_code,
                        "url": detail_json_url,
                        "error": "",
                    }
                last_reason = "detail_json_invalid"
            last_status_code = res.status_code
            last_error = res.error

    for url in build_hextech_detail_urls(c_id):
        last_url = url
        try:
            res = fetch_with_retry(
                url,
                timeout=timeout,
                quiet=True,
                raise_on_failure=True,
                caller="hextech_detail",
                context=f"championId={c_id};champion={c_name}",
            )
        except RemoteFetchError as error:
            last_reason = error.reason
            last_status_code = error.status_code
            last_url = error.url or url
            last_error = error.error
            if _is_deferred_remote_failure(error.reason, error.status_code):
                break
            continue
        if res is None or res.status_code != 200 or not res.text:
            last_reason = "empty_response"
            last_status_code = getattr(res, "status_code", None)
            last_error = getattr(res, "error", "")
            continue
        try:
            rows = extract_champion_stats(res.text, aug_id_map, truth_dict, c_id, c_name, champ, aug_tier_map)
            if rows:
                return {
                    "champ": champ,
                    "name": c_name,
                    "rows": rows,
                    "reason": "",
                    "status_code": res.status_code,
                    "url": url,
                    "error": "",
                }
        except ValueError as error:
            logging.warning("[%s] aug 解析失败：%s | URL=%s | 响应长度=%s", c_name, error, url, len(res.text))
            last_reason = "parse_error"
            last_status_code = res.status_code
            continue
        last_reason = "no_valid_rows"
        last_status_code = res.status_code

    return {
        "champ": champ,
        "name": c_name,
        "rows": [],
        "reason": last_reason,
        "status_code": last_status_code,
        "url": last_url,
        "error": last_error,
    }
