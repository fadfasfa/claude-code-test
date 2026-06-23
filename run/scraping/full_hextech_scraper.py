"""英雄海克斯排名抓取器。"""

import requests
import json
import time
import pandas as pd
from datetime import datetime, timezone
import os
import glob
import re
import logging
import threading
import random
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from processing.runtime_store import (
    CSV_ENCODING,
    build_daily_csv_path,
    build_runtime_state_path,
    get_latest_valid_csv,
    get_runtime_hextech_data_dir,
    resolve_runtime_data_file,
)
from scraping.version_sync import (
    HEXTECH_AUGMENT_METADATA_URLS,
    HEXTECH_CHAMPION_STATS_URLS,
    build_hextech_detail_urls,
    get_advanced_session,
    load_augment_map,
    load_champion_core_data,
)
from tools.atomic_io import atomic_write_csv, atomic_write_json
from tools.log_utils import log_task_summary

FRESHNESS_THRESHOLD = 0.0005
SCRAPER_BLOCKED_COOLDOWN_SECONDS = 6 * 60 * 60
BLOCKED_HTTP_STATUS_CODES = {403, 429}


class RemoteFetchError(RuntimeError):
    """远端请求不可用；由调用方统一执行本地回退，避免逐英雄刷屏。"""

    def __init__(self, reason: str, *, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code

# 请求标识池。
USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def get_random_ua():
    # 随机选择请求标识。
    return random.choice(USER_AGENT_POOL)


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
        return (
            _source_tier_value(stats.get("tier")),
            -_to_float(stats.get("win_rate", stats.get("winRate"))),
            -_to_float(stats.get("pick_rate", stats.get("pickRate"))),
            str(aug_id),
        )

    return sorted(augments.items(), key=sort_key)


def fetch_with_retry(
    session,
    url,
    max_retries=1,
    timeout=6,
    *,
    quiet: bool = False,
    raise_on_failure: bool = False,
):
    # 指数退避重试。
    for attempt in range(max_retries):
        try:
            headers = {"User-Agent": get_random_ua()}
            response = session.get(url, headers=headers, timeout=timeout, verify=True)
            response.raise_for_status()
            return response
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code:
                reason = f"http_{status_code}"
            elif isinstance(e, requests.exceptions.Timeout):
                reason = "timeout"
            else:
                reason = "network_error"
            if attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                if not quiet:
                    logging.warning(f"请求 {url} 失败 (尝试 {attempt + 1}/{max_retries}): {e}，{wait_time}秒后重试...")
                time.sleep(wait_time)
            else:
                if raise_on_failure:
                    raise RemoteFetchError(reason, status_code=status_code) from e
                if not quiet:
                    logging.warning(f"请求 {url} 失败，已达最大重试次数 {max_retries}: {e}")
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


def _write_scraper_status(result: str, reason: str = "", *, active_csv: str = "") -> dict:
    now = time.time()
    previous = load_scraper_status()
    payload = dict(previous)
    payload.update(
        {
            "last_attempt_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
            "last_result": result,
            "reason": reason,
            "blocked_until": "",
            "active_csv": active_csv,
            "active_csv_mtime": os.path.getmtime(active_csv) if active_csv and os.path.exists(active_csv) else 0.0,
            "last_success_time": previous.get("last_success_time", 0),
        }
    )
    if result == "success":
        payload["last_success_time"] = now
    elif result == "fallback" and reason in {"http_403", "http_429"}:
        blocked_until = datetime.fromtimestamp(now + SCRAPER_BLOCKED_COOLDOWN_SECONDS, tz=timezone.utc)
        payload["blocked_until"] = blocked_until.isoformat(timespec="seconds")
    atomic_write_json(build_runtime_state_path("scraper_status.json"), payload, ensure_ascii=False, indent=2)
    return payload


def _finish_refresh_failure(reason: str, *, started_at: float) -> bool:
    active_csv = get_latest_valid_csv() or ""
    if active_csv:
        _write_scraper_status("fallback", reason, active_csv=active_csv)
        logging.warning("海克斯远端刷新失败（%s），已使用 %s", reason, os.path.basename(active_csv))
        return True
    _write_scraper_status("failed", reason, active_csv="")
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
        return False, "远端处于 6 小时冷却期，继续使用本地有效 CSV。"
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

def update_status_file(active_csv: str = ""):
    """保留旧函数入口，并写入扩展后的成功状态。"""

    return _write_scraper_status("success", active_csv=active_csv)

def cleanup_old_csvs():
    # 清理过期数据和残留临时文件。
    csv_dir = get_runtime_hextech_data_dir()
    files = glob.glob(str(csv_dir / "Hextech_Data_*.csv"))
    tmp_files = glob.glob(str(csv_dir / ".Hextech_Data_*.csv.tmp"))
    now = datetime.now()

    for f in files + tmp_files:
        try:
            m = re.search(r"Hextech_Data_(\d{4}-\d{2}-\d{2})", os.path.basename(f))
            if not m: continue
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d")

            is_stale_csv = f.endswith('.csv') and (now - file_date).days > 3
            is_stale_tmp = f.endswith('.tmp') and (now - file_date).days > 1

            if is_stale_csv or is_stale_tmp:
                os.remove(f)
                logging.info(f"已清理过期/残留文件：{os.path.basename(f)}")
        except Exception as e:
            logging.error(f"清理文件异常 {f}: {e}")


def rebuild_runtime_caches() -> None:
    """新 CSV 发布后同步重建 Web 与 overlay 的运行缓存。"""

    from processing.precomputed_cache import rebuild_precomputed_api_cache_from_latest_csv
    from processing.overlay_hint_cache import build_overlay_hint_cache_from_precomputed, write_overlay_hint_cache

    rebuild_precomputed_api_cache_from_latest_csv()
    write_overlay_hint_cache(
        build_overlay_hint_cache_from_precomputed(source_tag="runtime-refresh")
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
    # 只解析当前英雄组件引用的 React Flight augments 数据块，避免整页统计串源。
    rows = []
    source_augments = _extract_react_flight_augments(html, champ_id)
    if not source_augments:
        logging.warning("[%s] 未解析到当前英雄 React Flight augments 数据块", champ_name)
        return rows

    for source_rank, (raw_id, raw_stats) in enumerate(_sort_source_augments(source_augments), start=1):
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
            if web_name and win > 0:
                rows.append({
                    "英雄 ID": champ_id,
                    "英雄名称": champ_name,
                    "英雄评级": champ_data.get('tier', 'T3'),
                    "英雄胜率": float(champ_data.get('winRate', 0)),
                    "英雄出场率": float(champ_data.get('pickRate', 0)),
                    "海克斯ID": mid,
                    "源站排名": source_rank,
                    "源站层级": _source_tier_label(stats.get("tier")),
                    "海克斯阶级": local_tier,
                    "海克斯名称": web_name,
                    "海克斯胜率": win,
                    "海克斯出场率": pick
                })
        except (ValueError, IndexError, AttributeError) as e:
            logging.warning(
                f"[{champ_name}] 海克斯 ID={mid} 解析失败：{e} | "
                f"源站字段：{stats} | 堆栈：{traceback.format_exc().strip()}"
            )
            continue

    return rows

def main_scraper(stop_event=None, force: bool = False):
    started_at = time.time()
    current_date = datetime.now().strftime('%Y-%m-%d')
    output_csv = build_daily_csv_path(current_date)

    can_run, msg = check_execution_permission(force=force)
    if not can_run:
        logging.info("海克斯抓取跳过：%s", msg)
        return bool(get_latest_valid_csv())

    logging.info("海克斯抓取开始：%s", msg)
    truth_dict = load_augment_map()
    core_data = load_champion_core_data()
    if not truth_dict or not core_data:
        return _finish_refresh_failure("base_data_missing", started_at=started_at)

    session = get_advanced_session()

    try:
        aug_response = None
        for url in HEXTECH_AUGMENT_METADATA_URLS:
            candidate = fetch_with_retry(session, url, quiet=True, raise_on_failure=True)
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), dict):
                    aug_response = candidate
                    break
            except Exception:
                continue
        if aug_response is None:
            return _finish_refresh_failure("metadata_invalid", started_at=started_at)
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
        for url in HEXTECH_CHAMPION_STATS_URLS:
            candidate = fetch_with_retry(session, url, quiet=True, raise_on_failure=True)
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), list):
                    stats_response = candidate
                    break
            except Exception:
                continue
        if stats_response is None:
            return _finish_refresh_failure("stats_invalid", started_at=started_at)
        stats_list = stats_response.json()
        if not stats_list:
            return _finish_refresh_failure("stats_empty", started_at=started_at)
    except RemoteFetchError as e:
        return _finish_refresh_failure(e.reason, started_at=started_at)
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return _finish_refresh_failure("handshake_invalid", started_at=started_at)

    # 先请求一个英雄详情。源站封禁时在创建 16 worker 线程池前熔断。
    preflight_champ = stats_list[0]
    preflight_id = str(preflight_champ.get("championId", ""))
    preflight_response = None
    try:
        for candidate_url in build_hextech_detail_urls(preflight_id):
            candidate = fetch_with_retry(session, candidate_url, quiet=True, raise_on_failure=True)
            if candidate.status_code == 200 and candidate.text:
                preflight_response = candidate
                break
    except RemoteFetchError as e:
        return _finish_refresh_failure(e.reason, started_at=started_at)
    if preflight_response is None:
        return _finish_refresh_failure("preflight_empty", started_at=started_at)
    preflight_name = core_data.get(preflight_id, {}).get("name", preflight_id)
    try:
        preflight_rows = extract_champion_stats(
            preflight_response.text,
            aug_id_map,
            truth_dict,
            preflight_id,
            preflight_name,
            preflight_champ,
            aug_tier_map,
        )
    except ValueError:
        return _finish_refresh_failure("preflight_parse_error", started_at=started_at)
    if not preflight_rows:
        return _finish_refresh_failure("preflight_no_valid_rows", started_at=started_at)

    all_rows = []
    lock = threading.Lock()
    remote_failure = {"reason": ""}
    remote_failure_event = threading.Event()

    def fetch_champ(champ):
        c_id = str(champ.get('championId', ''))
        c_name = core_data.get(c_id, {}).get("name", c_id)
        champ_rows = []
        url = ""
        try:
            if remote_failure_event.is_set():
                return c_name, champ_rows
            if c_id == preflight_id:
                return c_name, list(preflight_rows)
            else:
                time.sleep(random.uniform(0.5, 1.5))
                res = None
                for candidate_url in build_hextech_detail_urls(c_id):
                    url = candidate_url
                    res = fetch_with_retry(
                        session,
                        url,
                        quiet=True,
                        raise_on_failure=True,
                    )
                    if res is not None and res.status_code == 200 and res.text:
                        break

            if res is not None and res.status_code == 200 and len(res.text) > 0:
                try:
                    champ_rows = extract_champion_stats(
                        res.text,
                        aug_id_map,
                        truth_dict,
                        c_id,
                        c_name,
                        champ,
                        aug_tier_map
                    )
                except ValueError as e:
                    logging.warning(f"[{c_name}] aug 解析失败：{e} | URL={url} | 响应长度={len(res.text)}")
        except RemoteFetchError as e:
            with lock:
                if not remote_failure["reason"]:
                    remote_failure["reason"] = e.reason
            remote_failure_event.set()
        except requests.RequestException:
            with lock:
                if not remote_failure["reason"]:
                    remote_failure["reason"] = "network_error"
            remote_failure_event.set()

        return c_name, champ_rows

    thread_pool_timeout_seconds = 30
    logging.info("Hextech scrape running: heroes=%s workers=%s timeout=%s", len(stats_list), 16, thread_pool_timeout_seconds)
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch_champ, c) for c in stats_list]
        try:
            for f in as_completed(futures, timeout=thread_pool_timeout_seconds):
                if stop_event and stop_event.is_set():
                    logging.info("Stop signal received; cancelling Hextech scrape workers...")
                    for fut in futures:
                        fut.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    return False

                try:
                    _, rows = f.result()
                    with lock:
                        if rows:
                            all_rows.extend(rows)
                except Exception as e:
                    logging.error(f"Worker result collection failed: {e}")
        except TimeoutError:
            logging.error("Hextech scrape timed out; cancelling unfinished workers")
            for fut in futures:
                fut.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            remote_failure["reason"] = "thread_pool_timeout"
    if remote_failure["reason"]:
        return _finish_refresh_failure(remote_failure["reason"], started_at=started_at)
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
            return _finish_refresh_failure(f"insufficient_rows_{len(df)}", started_at=started_at)

        atomic_write_csv(output_csv, df, index=False, encoding=CSV_ENCODING)

        update_status_file(output_csv)
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
        return _finish_refresh_failure("no_valid_rows", started_at=started_at)

