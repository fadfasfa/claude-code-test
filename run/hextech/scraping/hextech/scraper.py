"""英雄海克斯排名抓取器。

调用方: core.refresh、scraping.augment_catalog、scraping.heal_worker; 关键依赖: runtime_store、BeautifulSoup、pandas。
"""

import json
import time
import pandas as pd
from datetime import datetime, timezone
import os
import glob
import re
import logging
import shutil
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path
from bs4 import BeautifulSoup
from hextech.catalog.runtime_store import (
    CSV_ENCODING,
    build_daily_csv_path,
    build_runtime_state_path,
    get_latest_valid_csv,
    get_runtime_hextech_data_dir,
    resolve_runtime_data_file,
)
from hextech.scraping.version_sync import (
    HEXTECH_AUGMENT_METADATA_URLS,
    HEXTECH_CHAMPION_STATS_URLS,
    build_hextech_detail_urls,
    load_augment_map,
    load_champion_core_data,
)
from hextech.scraping.transport.scrapling_client import ScraplingFetchResult, fetch_page, fetch_text
from hextech.support.atomic_io import atomic_write_csv, atomic_write_json
from hextech.support.log_utils import log_task_summary

FRESHNESS_THRESHOLD = 0.0005
SCRAPER_BLOCKED_COOLDOWN_SECONDS = 30 * 60
SCRAPER_REMOTE_FAILURE_ESCALATION_THRESHOLD = 3
BLOCKED_HTTP_STATUS_CODES = {403, 429}
DEFERRED_REMOTE_FAILURE_REASONS = {"http_403", "http_429", "timeout"}
DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL = "https://cdn.dtodo.cn/hextech/champion-details"
HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL = (
    os.getenv("HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL", DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL).strip()
    or DEFAULT_HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL
)
HEXTECH_DETAIL_WORKERS = 4
HEXTECH_DETAIL_RETRY_WORKERS = 2
HEXTECH_DETAIL_TIMEOUT_SECONDS = 6
HEXTECH_DETAIL_RETRY_TIMEOUT_SECONDS = 12
HEXTECH_DETAIL_RENDER_TIMEOUT_SECONDS = 20
HEXTECH_DETAIL_POOL_TIMEOUT_SECONDS = 180
HEXTECH_DETAIL_RETRY_POOL_TIMEOUT_SECONDS = 120
HEXTECH_HANDSHAKE_RETRIES = 2
HEXTECH_HANDSHAKE_TIMEOUT_SECONDS = 6
HEXTECH_BROWSER_DETAIL_FALLBACK_ENABLED = os.getenv("HEXTECH_BROWSER_DETAIL_FALLBACK", "").strip() == "1"
HEXTECH_ROOT_CSV_RETENTION_DAYS = 14
HEXTECH_BACKUP_CSV_RETENTION_DAYS = 7
HEXTECH_TMP_CSV_RETENTION_DAYS = 1


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

def _clean_augment_text(value) -> str:
    # 统一清洗文本字段，避免空白干扰后续拼接。
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _extract_augment_meta(raw_item: dict) -> dict:
    # 提取增强符文描述信息；tooltip 缺失时回退 description。
    description = _clean_augment_text(
        raw_item.get("description")
        or raw_item.get("desc")
    )
    tooltip = _clean_augment_text(
        raw_item.get("tooltip")
        or raw_item.get("toolTip")
        or raw_item.get("tips")
    )
    if not tooltip:
        tooltip = description
    spell_values = _extract_spell_values(raw_item)
    return {
        "description": description,
        "tooltip": tooltip,
        "spell_values": spell_values,
    }


def _extract_spell_values(raw_item: dict) -> dict:
    # 提取增强符文中的可替换数值，用于后续 tooltip_plain 占位符解析。
    values = {}

    def append_value(name, value):
        key = _clean_augment_text(name)
        if not key:
            return
        try:
            values[key] = float(value)
        except (TypeError, ValueError):
            return

    def consume_mapping(mapping):
        if not isinstance(mapping, dict):
            return
        for key, val in mapping.items():
            if isinstance(val, (int, float)):
                append_value(key, val)
            elif isinstance(val, list):
                # 兼容 [100, 120, ...] 这种多等级数组，取首个有效数值。
                for item in val:
                    if isinstance(item, (int, float)):
                        append_value(key, item)
                        break

    consume_mapping(raw_item.get("spellDataValues"))
    consume_mapping(raw_item.get("DataValues"))
    consume_mapping(raw_item.get("dataValues"))
    consume_mapping(raw_item.get("mDataValues"))

    effects = raw_item.get("mEffects")
    if isinstance(effects, dict):
        consume_mapping(effects)
    elif isinstance(effects, list):
        for entry in effects:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("key") or entry.get("id")
                val = entry.get("value") or entry.get("values") or entry.get("amount")
                if isinstance(val, list):
                    val = next((x for x in val if isinstance(x, (int, float))), None)
                if isinstance(val, (int, float)):
                    append_value(name, val)

    return values


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percent_text_to_rate(value: str) -> float:
    text = str(value or "").strip().replace(" ", "")
    if not text.endswith("%"):
        return 0.0
    return _to_float(text.removesuffix("%")) / 100.0


def _source_tier_value(value) -> int:
    text = str(value or "").strip().upper().removeprefix("T")
    try:
        return int(text)
    except ValueError:
        return 99


def _source_tier_label(value) -> str:
    tier = _source_tier_value(value)
    return f"T{tier}" if tier != 99 else str(value or "").strip()


def _metadata_tier_from_rarity(value) -> str:
    rarity_to_tier = {
        0: "白银",
        1: "黄金",
        2: "棱彩",
        3: "棱彩",
        "0": "白银",
        "1": "黄金",
        "2": "棱彩",
        "3": "棱彩",
        "silver": "白银",
        "gold": "黄金",
        "prismatic": "棱彩",
        "白银": "白银",
        "黄金": "黄金",
        "棱彩": "棱彩",
    }
    return rarity_to_tier.get(value, rarity_to_tier.get(str(value or "").strip().lower(), ""))


def _decode_next_flight_payloads(html: str) -> list[str]:
    # Next/React Flight 会把长文本切成多段 push 字符串；这里仅做字符串反转义，不解释页面其它文本。
    payloads = []
    pattern = re.compile(
        r"<script>self\.__next_f\.push\(\[1,\"(.*?)\"\]\)</script>",
        re.DOTALL,
    )
    for match in pattern.finditer(html or ""):
        raw_payload = match.group(1)
        try:
            payloads.append(json.loads(f'"{raw_payload}"'))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logging.debug("React Flight payload 反转义失败：%s", exc)
    return payloads


def _extract_champion_augments_ref_id(payloads: list[str], champ_id: str) -> str:
    escaped_id = re.escape(str(champ_id))
    ref_pattern = re.compile(
        rf'"championAugmentsStats"\s*:\s*\{{\s*"{escaped_id}"\s*:\s*\[\s*\[\s*"{escaped_id}"\s*,\s*"\$([0-9A-Za-z]+)"',
        re.DOTALL,
    )
    for payload in payloads:
        match = ref_pattern.search(payload)
        if match:
            return match.group(1)
    return ""


def _extract_flight_text_blocks(payloads: list[str]) -> dict[str, str]:
    # Flight 文本块形如 `29:T9159,`，下一段 push 字符串才是这个 ID 对应的正文。
    blocks = {}
    pending_ref = ""
    ref_pattern = re.compile(r"(?:^|\n)([0-9A-Za-z]+):T[0-9A-Fa-f]+,\s*$")
    for payload in payloads:
        if pending_ref and payload.lstrip().startswith("{"):
            blocks[pending_ref] = payload.strip()
            pending_ref = ""

        match = ref_pattern.search(payload)
        if match:
            pending_ref = match.group(1)
    return blocks


def _parse_augments_payload(payload: str) -> dict:
    text = str(payload or "").strip()
    if not text:
        return {}

    start = text.find('{"augments"')
    if start > 0:
        text = text[start:]

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    augments = parsed.get("augments") if isinstance(parsed, dict) else None
    if not isinstance(augments, dict):
        return {}
    return augments


def _looks_like_augment_stats(augments: dict) -> bool:
    """校验 dict 是否为 id→统计数据 结构（排除 SEO 元数据块）。

    SEO 块如 {"augments":{"title":"...","description":"..."}} 的 value 是纯字符串；
    真实 augment stats 块 value 是含 win_rate/winRate/pick_rate/pickRate/tier 的字典。
    """
    if not isinstance(augments, dict):
        return False
    for value in augments.values():
        if isinstance(value, dict) and (
            "win_rate" in value or "winRate" in value
            or "pick_rate" in value or "pickRate" in value
            or "tier" in value
        ):
            return True
    return False


def _extract_react_flight_augments(html: str, champ_id: str) -> dict:
    payloads = _decode_next_flight_payloads(html)
    if not payloads:
        return {}

    ref_id = _extract_champion_augments_ref_id(payloads, champ_id)
    if ref_id:
        augments = _parse_augments_payload(_extract_flight_text_blocks(payloads).get(ref_id, ""))
        if augments:
            return augments

    # 兼容极简测试快照或站点 Flight 形态变化：仍只读取 React Flight 内唯一的 augments JSON 块。
    # 加 _looks_like_augment_stats 校验排除 SEO 元数据块（其 value 为纯字符串，不含统计字段）。
    candidates = [_parse_augments_payload(payload) for payload in payloads if '{"augments"' in payload]
    candidates = [item for item in candidates if item and _looks_like_augment_stats(item)]
    if len(candidates) == 1:
        logging.warning("[%s] 未找到 championAugmentsStats 文本块，使用唯一 React Flight augments 块兜底。", champ_id)
        return candidates[0]
    return {}


def _sort_source_augments(augments: dict) -> list[tuple[str, dict]]:
    def sort_key(entry):
        aug_id, raw_stats = entry
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        rank = int(_to_float(stats.get("rank"), default=0.0))
        return (
            rank if rank > 0 else 9999,
            _source_tier_value(stats.get("tier")),
            -_to_float(stats.get("win_rate", stats.get("winRate"))),
            -_to_float(stats.get("pick_rate", stats.get("pickRate"))),
            str(aug_id),
        )

    return sorted(augments.items(), key=sort_key)


def build_hextech_champion_detail_json_url(champ_id: str) -> str:
    return f"{HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL.rstrip('/')}/{str(champ_id).strip()}.json"


def _extract_detail_json_augments(payload: dict, champ_id: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    candidates = payload.get("championAugments")
    if not isinstance(candidates, list):
        return {}
    expected_id = str(champ_id)
    for item in candidates:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        item_champ_id = str(item[0])
        if item_champ_id != expected_id:
            continue
        raw_stats = item[1]
        if isinstance(raw_stats, str):
            try:
                parsed = json.loads(raw_stats)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        elif isinstance(raw_stats, dict):
            parsed = raw_stats
        else:
            continue
        augments = parsed.get("augments") if isinstance(parsed, dict) else None
        if isinstance(augments, dict) and _looks_like_augment_stats(augments):
            return augments
    return {}


def _source_total_from_augments(augments: dict) -> int:
    totals = []
    for raw_stats in augments.values():
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        total = _to_float(stats.get("total"), default=0.0)
        if total > 0:
            totals.append(int(total))
    return max(totals, default=0)


def _rows_from_source_augments(
    source_augments: dict,
    *,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    rows = []
    for fallback_rank, (raw_id, raw_stats) in enumerate(_sort_source_augments(source_augments), start=1):
        mid = str(raw_id)
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        try:
            win = _to_float(stats.get("win_rate", stats.get("winRate")))
            pick = _to_float(stats.get("pick_rate", stats.get("pickRate")))

            if pick > 1.0:
                pick = pick / 100.0
                logging.debug(f"[量纲转换] 海克斯 ID={mid}，出场率从百分数转换为小数：{pick*100:.1f}% -> {pick:.4f}")
            pick = min(1.0, max(0.0, pick))

            web_name = aug_id_map.get(mid, "")
            local_tier = truth_dict.get(web_name) or (aug_tier_map or {}).get(mid) or "未知"
            source_rank = int(_to_float(stats.get("rank"), default=float(fallback_rank))) or fallback_rank
            if web_name and win > 0:
                rows.append(
                    _build_row(
                        champ_id=champ_id,
                        champ_name=champ_name,
                        champ_data=champ_data,
                        augment_id=mid,
                        augment_name=web_name,
                        source_rank=source_rank,
                        source_tier=stats.get("tier"),
                        local_tier=local_tier,
                        winrate=win,
                        pickrate=pick,
                    )
                )
        except (ValueError, IndexError, AttributeError) as e:
            logging.warning(
                f"[{champ_name}] 海克斯 ID={mid} 解析失败：{e} | "
                f"源站字段：{stats} | 堆栈：{traceback.format_exc().strip()}"
            )
            continue
    return sorted(rows, key=lambda item: item["源站排名"])


def extract_champion_detail_json_stats(
    payload: dict,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    """解析 CDN champion-details JSON；这是全量海克斯统计的快速链路。"""

    source_augments = _extract_detail_json_augments(payload, champ_id)
    if not source_augments:
        return []
    return _rows_from_source_augments(
        source_augments,
        aug_id_map=aug_id_map,
        truth_dict=truth_dict,
        champ_id=champ_id,
        champ_name=champ_name,
        champ_data=champ_data,
        aug_tier_map=aug_tier_map,
    )


def _build_row(
    *,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    augment_id: str,
    augment_name: str,
    source_rank: int,
    source_tier: str,
    local_tier: str,
    winrate: float,
    pickrate: float,
) -> dict:
    return {
        "英雄 ID": champ_id,
        "英雄名称": champ_name,
        "英雄评级": champ_data.get('tier', 'T3'),
        "英雄胜率": float(champ_data.get('winRate', 0)),
        "英雄出场率": float(champ_data.get('pickRate', 0)),
        "海克斯ID": str(augment_id),
        "源站排名": int(source_rank),
        "源站层级": _source_tier_label(source_tier),
        "海克斯阶级": local_tier,
        "海克斯名称": augment_name,
        "海克斯胜率": winrate,
        "海克斯出场率": min(1.0, max(0.0, pickrate)),
    }


def _extract_rendered_table_stats(
    html: str,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list[dict]:
    """解析渲染后的隐藏全量表格；静态 React Flight 只包含折叠窗口。"""

    id_by_name = {str(name): str(augment_id) for augment_id, name in aug_id_map.items() if str(name or "").strip()}
    if not id_by_name:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    seen_ids = set()
    for tr in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"], recursive=False)]
        if len(cells) != 5:
            continue
        rank_text, augment_name, source_tier, winrate_text, pickrate_text = cells
        if not rank_text.isdigit() or not source_tier.replace(" ", "").upper().startswith("T"):
            continue
        if not winrate_text.strip().endswith("%") or not pickrate_text.strip().endswith("%"):
            continue
        augment_name = _clean_augment_text(augment_name)
        augment_id = id_by_name.get(augment_name, "")
        if not augment_id or augment_id in seen_ids:
            continue
        winrate = _percent_text_to_rate(winrate_text)
        pickrate = _percent_text_to_rate(pickrate_text)
        if winrate <= 0:
            continue
        seen_ids.add(augment_id)
        local_tier = truth_dict.get(augment_name) or (aug_tier_map or {}).get(augment_id) or "未知"
        rows.append(
            _build_row(
                champ_id=champ_id,
                champ_name=champ_name,
                champ_data=champ_data,
                augment_id=augment_id,
                augment_name=augment_name,
                source_rank=int(rank_text),
                source_tier=source_tier,
                local_tier=local_tier,
                winrate=winrate,
                pickrate=pickrate,
            )
        )
    return sorted(rows, key=lambda item: item["源站排名"])


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
    if status_code in BLOCKED_HTTP_STATUS_CODES:
        return True
    return reason in DEFERRED_REMOTE_FAILURE_REASONS


def _is_deferred_remote_failure(reason: str, status_code: int | None = None) -> bool:
    return _is_blocking_remote_failure(reason, status_code)


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
        "scraper_status.json",
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
                "连续远端失败达到阈值；先保留证据，后续可评估低风险 header/rate limit 或授权 stealth 兜底。"
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
        "scraper_status.json",
    )
    now = time.time()
    current_csv = build_daily_csv_path(datetime.now().strftime('%Y-%m-%d'))
    if not os.path.exists(current_csv):
        return True, "今日战报 CSV 缺失，启动抓取..."
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
    """保留旧函数入口，并写入扩展后的成功状态。"""

    attempt = _finish_attempt(
        attempt,
        result="success",
        reason="",
        active_csv=active_csv,
        output_csv=active_csv,
        fallback_used=False,
    )
    return _write_scraper_status("success", active_csv=active_csv, attempt=attempt)

def cleanup_old_csvs():
    # 根目录 CSV 是回退材料，保留较长窗口；backups/ 和 tmp 使用更短窗口。
    csv_dir = get_runtime_hextech_data_dir()
    retention_groups = [
        (glob.glob(str(csv_dir / "Hextech_Data_*.csv")), HEXTECH_ROOT_CSV_RETENTION_DAYS, "历史 CSV"),
        (glob.glob(str(csv_dir / "backups" / "Hextech_Data_*.csv")), HEXTECH_BACKUP_CSV_RETENTION_DAYS, "备份 CSV"),
        (glob.glob(str(csv_dir / ".Hextech_Data_*.csv.tmp")), HEXTECH_TMP_CSV_RETENTION_DAYS, "临时文件"),
    ]
    now = datetime.now()

    for files, retention_days, label in retention_groups:
        for f in files:
            try:
                m = re.search(r"Hextech_Data_(\d{4}-\d{2}-\d{2})", os.path.basename(f))
                if not m:
                    continue
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d")

                if (now - file_date).days > retention_days:
                    os.remove(f)
                    logging.info("已清理过期%s：%s", label, os.path.basename(f))
            except Exception as e:
                logging.error(f"清理文件异常 {f}: {e}")


def backup_active_csv_before_publish(output_csv: str) -> str:
    """刷新前保留当前有效 CSV；备份失败必须阻止发布。"""

    active_csv = get_latest_valid_csv() or ""
    if not active_csv or not os.path.exists(active_csv):
        return ""
    if os.path.abspath(active_csv) == os.path.abspath(output_csv) and not os.path.exists(output_csv):
        return ""
    backup_dir = get_runtime_hextech_data_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{Path(active_csv).stem}.backup-{stamp}.csv"
    shutil.copy2(active_csv, backup_path)
    logging.info("刷新前已备份当前 CSV：source=%s backup=%s", os.path.basename(active_csv), backup_path)
    return str(backup_path)


def rebuild_runtime_caches() -> None:
    """新 CSV 发布后同步重建 Web 与 overlay 的运行缓存。"""

    from hextech.catalog.precomputed_cache import rebuild_precomputed_api_cache_from_latest_csv
    from hextech.core.settings import load_ui_feature_flags
    from hextech.overlay.hints import build_overlay_hint_cache_from_precomputed, write_overlay_hint_cache

    flags = load_ui_feature_flags()
    include_private_stats = bool(flags.get("private_policy_stats_enabled", False))
    rebuild_precomputed_api_cache_from_latest_csv()
    write_overlay_hint_cache(
        build_overlay_hint_cache_from_precomputed(
            include_private_stats=include_private_stats,
            source_tag="runtime-refresh",
        )
    )


def extract_champion_stats(
    html: str,
    aug_id_map: dict,
    truth_dict: dict,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_tier_map: dict | None = None,
) -> list:
    rendered_rows = _extract_rendered_table_stats(
        html,
        aug_id_map,
        truth_dict,
        champ_id,
        champ_name,
        champ_data,
        aug_tier_map,
    )
    if rendered_rows:
        return rendered_rows

    # 静态响应只解析当前英雄组件引用的 React Flight augments 数据块，避免整页统计串源。
    rows = []
    source_augments = _extract_react_flight_augments(html, champ_id)
    if not source_augments:
        logging.warning("[%s] 未解析到当前英雄 React Flight augments 数据块", champ_name)
        return rows

    rows = _rows_from_source_augments(
        source_augments,
        aug_id_map=aug_id_map,
        truth_dict=truth_dict,
        champ_id=champ_id,
        champ_name=champ_name,
        champ_data=champ_data,
        aug_tier_map=aug_tier_map,
    )
    return rows


def _rendered_detail_rows(
    url: str,
    *,
    champ_id: str,
    champ_name: str,
    champ_data: dict,
    aug_id_map: dict,
    truth_dict: dict,
    aug_tier_map: dict | None,
) -> list[dict]:
    result = fetch_page(
        url,
        mode="browser",
        timeout_ms=HEXTECH_DETAIL_RENDER_TIMEOUT_SECONDS * 1000,
        network_idle=True,
        max_attempts=1,
    )
    if result.error or result.status_code != 200 or not result.html:
        logging.warning(
            "[%s] browser 详情页兜底失败：championId=%s url=%s status=%s reason=%s error=%s",
            champ_name,
            champ_id,
            url,
            result.status_code,
            result.error_kind,
            result.error,
        )
        return []
    return extract_champion_stats(
        result.html,
        aug_id_map,
        truth_dict,
        champ_id,
        champ_name,
        champ_data,
        aug_tier_map,
    )


def fetch_champion_detail_stats_fast(
    champ: dict,
    *,
    core_data: dict,
    aug_id_map: dict,
    truth_dict: dict,
    aug_tier_map: dict | None,
    timeout: int,
    allow_browser_fallback: bool = False,
) -> dict:
    """优先使用 CDN JSON 全量快链路；页面和 browser 只作回退。"""

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
    except RemoteFetchError as e:
        last_reason = e.reason
        last_status_code = e.status_code
        last_url = e.url or detail_json_url
        last_error = e.error
        if _is_deferred_remote_failure(e.reason, e.status_code):
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
                    res.json(),
                    aug_id_map,
                    truth_dict,
                    c_id,
                    c_name,
                    champ,
                    aug_tier_map,
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
                # 如果 CDN 形态变化为 HTML，仍尝试复用页面解析；不直接升级 browser。
                rows = extract_champion_stats(
                    res.text,
                    aug_id_map,
                    truth_dict,
                    c_id,
                    c_name,
                    champ,
                    aug_tier_map,
                )
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
        except RemoteFetchError as e:
            last_reason = e.reason
            last_status_code = e.status_code
            last_url = e.url or url
            last_error = e.error
            if _is_deferred_remote_failure(e.reason, e.status_code):
                break
            continue
        if res is None or res.status_code != 200 or not res.text:
            last_reason = "empty_response"
            last_status_code = getattr(res, "status_code", None)
            last_error = getattr(res, "error", "")
            continue
        try:
            rows = extract_champion_stats(
                res.text,
                aug_id_map,
                truth_dict,
                c_id,
                c_name,
                champ,
                aug_tier_map,
            )
            source_total = _source_total_from_augments(_extract_react_flight_augments(res.text, c_id))
            if rows and source_total > len(rows):
                logging.warning(
                    "[%s] HTML 快路径不完整：championId=%s static_rows=%s source_total=%s browser_fallback=%s",
                    c_name,
                    c_id,
                    len(rows),
                    source_total,
                    allow_browser_fallback,
                )
                if allow_browser_fallback:
                    rendered_rows = _rendered_detail_rows(
                        url,
                        champ_id=c_id,
                        champ_name=c_name,
                        champ_data=champ,
                        aug_id_map=aug_id_map,
                        truth_dict=truth_dict,
                        aug_tier_map=aug_tier_map,
                    )
                    if len(rendered_rows) > len(rows):
                        logging.info(
                            "[%s] browser 小范围兜底补全：championId=%s static_rows=%s rendered_rows=%s source_total=%s",
                            c_name,
                            c_id,
                            len(rows),
                            len(rendered_rows),
                            source_total,
                        )
                        rows = rendered_rows
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
        except ValueError as e:
            logging.warning(f"[{c_name}] aug 解析失败：{e} | URL={url} | 响应长度={len(res.text)}")
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


def main_scraper(stop_event=None, force: bool = False):
    started_at = time.time()
    attempt = _new_attempt_context()
    current_date = datetime.now().strftime('%Y-%m-%d')
    output_csv = build_daily_csv_path(current_date)
    attempt["output_csv"] = output_csv

    can_run, msg = check_execution_permission(force=force)
    if not can_run:
        logging.info("海克斯抓取跳过：%s", msg)
        return bool(get_latest_valid_csv())

    logging.info("海克斯抓取开始：%s", msg)
    truth_dict = load_augment_map()
    core_data = load_champion_core_data()
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
            allow_browser_fallback=HEXTECH_BROWSER_DETAIL_FALLBACK_ENABLED,
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
        executor = ThreadPoolExecutor(max_workers=workers)
        shutdown_called = False
        futures = []
        future_to_champ: dict = {}
        try:
            for champ in champs:
                c_id = str(champ.get("championId", ""))
                cached_rows = preflight_rows if c_id == preflight_id else None
                future = executor.submit(fetch_champ_detail, champ, timeout=timeout, preflight_rows=cached_rows)
                futures.append(future)
                future_to_champ[future] = champ
            try:
                for future in as_completed(futures, timeout=pool_timeout):
                    if stop_event and stop_event.is_set():
                        logging.info("Stop signal received; cancelling Hextech scrape workers...")
                        for fut in futures:
                            fut.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        shutdown_called = True
                        return pass_rows, failures, "stopped"

                    try:
                        item = future.result()
                    except Exception as e:
                        logging.error("Worker result collection failed: %s", e)
                        failure_item = {
                            "champ": {},
                            "name": "",
                            "rows": [],
                            "reason": "worker_error",
                            "status_code": None,
                            "url": "",
                            "error": str(e),
                        }
                        failures.append(failure_item)
                        attempt["completed_heroes"] = int(attempt.get("completed_heroes") or 0) + 1
                        _record_detail_result(attempt, failure_item)
                        continue
                    if item["rows"]:
                        pass_rows.extend(item["rows"])
                    else:
                        failures.append(item)
                    attempt["completed_heroes"] = int(attempt.get("completed_heroes") or 0) + 1
                    _record_detail_result(attempt, item)
            except TimeoutError:
                logging.error("Hextech detail pass timed out: label=%s", label)
                for fut in futures:
                    if fut.done():
                        continue
                    champ = future_to_champ.get(fut) or {}
                    c_id = str(champ.get("championId", ""))
                    failure_item = {
                        "champ": champ,
                        "name": core_data.get(c_id, {}).get("name", c_id),
                        "rows": [],
                        "reason": "thread_pool_timeout",
                        "status_code": None,
                        "url": "",
                        "error": f"detail pass timed out: {label}",
                    }
                    failures.append(failure_item)
                    _record_detail_result(attempt, failure_item)
                for fut in futures:
                    fut.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                shutdown_called = True
                return pass_rows, failures, "thread_pool_timeout"
        finally:
            if not shutdown_called:
                executor.shutdown(wait=True, cancel_futures=False)
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

        backup_active_csv_before_publish(output_csv)
        atomic_write_csv(output_csv, df, index=False, encoding=CSV_ENCODING)

        attempt["success_rows"] = len(df)
        update_status_file(output_csv, attempt=attempt)
        cleanup_old_csvs()
        try:
            rebuild_runtime_caches()
        except Exception:
            logging.exception("新 CSV 已发布，但缓存重建失败")
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

