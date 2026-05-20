from __future__ import annotations

"""海克斯与联动数据源完整性验收门。

用途：
- 随机抽样英雄并把本地 CSV、预计算缓存和本地 API payload 对齐到源站详情页数据。
- 可选校验 ApexLoL snapshot 解析出的右侧联动数据，缺少 snapshot 时直接失败。

边界：
- 不读取 cookie、storage、代理或登录态。
- 不写业务数据，只读取源站公开页面、本地 CSV、缓存和 snapshot。
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from display.web_api import _normalize_synergy_items, register_routes  # noqa: E402
from processing.precomputed_cache import (  # noqa: E402
    load_precomputed_champion_list,
    load_precomputed_hextech_for_hero,
)
from processing.runtime_store import build_synergy_data_path, get_latest_csv, load_runtime_csv  # noqa: E402
from scraping import full_hextech_scraper as hex_scraper  # noqa: E402
from scraping import full_synergy_scraper as synergy_scraper  # noqa: E402
from scraping.version_sync import (  # noqa: E402
    HEXTECH_AUGMENT_METADATA_URLS,
    HEXTECH_CHAMPION_STATS_URLS,
    build_hextech_detail_urls,
    get_advanced_session,
    load_champion_core_data,
)


FLOAT_TOLERANCE = 1e-10


def _normalize_name(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_json_from_candidates(session, urls: tuple[str, ...] | list[str], expected_type: type) -> Any:
    for url in urls:
        response = hex_scraper.fetch_with_retry(session, url, max_retries=1, timeout=10)
        if response is None:
            continue
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, expected_type):
            return payload
    raise RuntimeError(f"无法获取源站 JSON：{urls}")


def _build_augment_maps(aug_data: dict) -> tuple[dict[str, str], dict[str, str]]:
    truth_dict = hex_scraper.load_augment_map()
    aug_id_map: dict[str, str] = {}
    aug_tier_map: dict[str, str] = {}
    for raw_key, raw_item in aug_data.items():
        item = raw_item if isinstance(raw_item, dict) else {}
        aug_id = str(raw_key)
        display_name = hex_scraper._clean_augment_text(item.get("displayName"))
        aug_id_map[aug_id] = display_name
        aug_tier_map[aug_id] = truth_dict.get(display_name) or hex_scraper._metadata_tier_from_rarity(item.get("rarity"))
    return aug_id_map, aug_tier_map


def _resolve_hero_ids(raw_heroes: str, core_data: dict[str, dict]) -> list[str]:
    tokens = [item.strip() for item in raw_heroes.replace(",", " ").split() if item.strip()]
    if not tokens:
        return []

    lookup: dict[str, str] = {}
    for champ_id, info in core_data.items():
        values = [champ_id, info.get("name", ""), info.get("title", ""), info.get("en_name", ""), *(info.get("aliases") or [])]
        for value in values:
            key = _normalize_name(value)
            if key:
                lookup.setdefault(key, str(champ_id))

    resolved = []
    for token in tokens:
        champ_id = lookup.get(_normalize_name(token))
        if not champ_id:
            raise ValueError(f"无法解析英雄：{token}")
        if champ_id not in resolved:
            resolved.append(champ_id)
    return resolved


def _select_heroes(args, core_data: dict[str, dict]) -> tuple[int, list[str]]:
    if args.heroes:
        return int(args.seed or 0), _resolve_hero_ids(args.heroes, core_data)

    seed = int(args.seed if args.seed is not None else time.time())
    rng = random.Random(seed)
    hero_ids = sorted(core_data.keys(), key=lambda item: int(item) if str(item).isdigit() else str(item))
    sample_size = min(max(1, int(args.sample_size)), len(hero_ids))
    return seed, rng.sample(hero_ids, sample_size)


def _source_rows_for_hero(
    session,
    champ_id: str,
    core_data: dict[str, dict],
    stats_by_id: dict[str, dict],
    aug_id_map: dict[str, str],
    truth_dict: dict[str, str],
    aug_tier_map: dict[str, str],
) -> list[dict]:
    hero_info = core_data[champ_id]
    champ_data = stats_by_id.get(champ_id) or {
        "championId": champ_id,
        "tier": "T3",
        "winRate": 0,
        "pickRate": 0,
    }

    last_error = ""
    for url in build_hextech_detail_urls(champ_id):
        response = hex_scraper.fetch_with_retry(session, url, max_retries=1, timeout=12)
        if response is None or response.status_code != 200 or not response.text:
            last_error = f"无法读取 {url}"
            continue
        rows = hex_scraper.extract_champion_stats(
            response.text,
            aug_id_map,
            truth_dict,
            champ_id,
            hero_info.get("name", champ_id),
            champ_data,
            aug_tier_map,
        )
        if rows:
            return rows
        last_error = f"源站详情页无可解析 augments：{url}"
    raise RuntimeError(last_error or f"无法获取英雄源站详情：{champ_id}")


def _row_id(row: dict | pd.Series) -> str:
    value = row.get("海克斯ID", "")
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text.removesuffix(".0")


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= FLOAT_TOLERANCE
    except (TypeError, ValueError):
        return False


def _int_equal(left: Any, right: Any) -> bool:
    try:
        return int(float(left)) == int(float(right))
    except (TypeError, ValueError):
        return False


def _compare_csv_rows(source_rows: list[dict], csv_rows: pd.DataFrame) -> tuple[bool, list[str]]:
    issues: list[str] = []
    source_by_id = {_row_id(row): row for row in source_rows if _row_id(row)}
    csv_by_id = {_row_id(row): row for _, row in csv_rows.iterrows() if _row_id(row)}

    missing = sorted(set(source_by_id) - set(csv_by_id))
    extra = sorted(set(csv_by_id) - set(source_by_id))
    if missing:
        issues.append(f"CSV 缺失海克斯ID：{missing[:10]}")
    if extra:
        issues.append(f"CSV 多出海克斯ID：{extra[:10]}")
    if len(source_by_id) != len(csv_by_id):
        issues.append(f"CSV 条数不一致：source={len(source_by_id)} csv={len(csv_by_id)}")

    for aug_id in sorted(set(source_by_id) & set(csv_by_id), key=lambda item: int(item) if item.isdigit() else item):
        src = source_by_id[aug_id]
        local = csv_by_id[aug_id]
        checks = [
            ("海克斯名称", str(src.get("海克斯名称", "")), str(local.get("海克斯名称", ""))),
            ("海克斯阶级", str(src.get("海克斯阶级", "")), str(local.get("海克斯阶级", ""))),
            ("源站层级", str(src.get("源站层级", "")), str(local.get("源站层级", ""))),
        ]
        for field, expected, actual in checks:
            if expected != actual:
                issues.append(f"{aug_id} {field} 不一致：source={expected} csv={actual}")
        if not _int_equal(src.get("源站排名"), local.get("源站排名")):
            issues.append(f"{aug_id} 源站排名不一致：source={src.get('源站排名')} csv={local.get('源站排名')}")
        if not _float_equal(src.get("海克斯胜率"), local.get("海克斯胜率")):
            issues.append(f"{aug_id} 海克斯胜率不一致：source={src.get('海克斯胜率')} csv={local.get('海克斯胜率')}")
        if not _float_equal(src.get("海克斯出场率"), local.get("海克斯出场率")):
            issues.append(f"{aug_id} 海克斯出场率不一致：source={src.get('海克斯出场率')} csv={local.get('海克斯出场率')}")
    return not issues, issues


def _cards_by_id(payload: dict, key: str = "comprehensive") -> dict[str, dict]:
    cards = payload.get(key) if isinstance(payload, dict) else []
    if not isinstance(cards, list):
        return {}
    return {_row_id(card): card for card in cards if _row_id(card)}


def _compare_payload_cards(source_rows: list[dict], payload: dict, label: str) -> tuple[bool, list[str]]:
    issues: list[str] = []
    source_by_id = {_row_id(row): row for row in source_rows if _row_id(row)}
    cards_by_id = _cards_by_id(payload)
    missing = sorted(set(source_by_id) - set(cards_by_id))
    extra = sorted(set(cards_by_id) - set(source_by_id))
    if missing:
        issues.append(f"{label} 缺失海克斯ID：{missing[:10]}")
    if extra:
        issues.append(f"{label} 多出海克斯ID：{extra[:10]}")
    if len(source_by_id) != len(cards_by_id):
        issues.append(f"{label} 条数不一致：source={len(source_by_id)} payload={len(cards_by_id)}")

    for aug_id in sorted(set(source_by_id) & set(cards_by_id), key=lambda item: int(item) if item.isdigit() else item):
        src = source_by_id[aug_id]
        card = cards_by_id[aug_id]
        if not _float_equal(src.get("海克斯胜率"), card.get("海克斯胜率")):
            issues.append(f"{label} {aug_id} 胜率不一致：source={src.get('海克斯胜率')} payload={card.get('海克斯胜率')}")
        if not _float_equal(src.get("海克斯出场率"), card.get("海克斯出场率")):
            issues.append(f"{label} {aug_id} 登场率不一致：source={src.get('海克斯出场率')} payload={card.get('海克斯出场率')}")
        if not _int_equal(src.get("源站排名"), card.get("源站排名")):
            issues.append(f"{label} {aug_id} 源站排名不一致：source={src.get('源站排名')} payload={card.get('源站排名')}")
    return not issues, issues


def _build_api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _load_api_payload(client, hero_name: str) -> dict:
    response = client.get(f"/api/champion/{quote(hero_name, safe='')}/hextechs")
    if response.status_code != 200:
        raise RuntimeError(f"本地 API 返回 {response.status_code}: {response.text[:200]}")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _synergy_item_key(item: dict) -> tuple:
    normalized = _normalize_synergy_items([item], [])
    value = normalized[0] if normalized else item
    return (
        tuple(value.get("augment_names") or []),
        str(value.get("tier") or ""),
        str(value.get("rating") or ""),
        str(value.get("tag") or ""),
        str(value.get("author") or ""),
        bool(value.get("is_original")),
        str(value.get("content") or ""),
    )


def _load_snapshot_synergy_payload(core_data: dict[str, dict]) -> tuple[dict | None, str]:
    source = synergy_scraper.ApexSource()
    resources = source._load_snapshot_resources()
    if not resources:
        return None, "no_apex_snapshot_resources"

    core_info = synergy_scraper.build_core_info(core_data)
    extractor = synergy_scraper.SynergyExtractor(
        champion_lookup=synergy_scraper.build_champion_lookup(core_info),
        augment_name_map=synergy_scraper.build_augment_name_map_from_static(),
    )
    try:
        synergy_map = extractor.extract(resources)
    except ValueError as exc:
        return None, f"snapshot_synergy_empty:{exc}"
    if not synergy_map:
        return None, "snapshot_synergy_empty"
    payload = synergy_scraper.SynergyWriter(core_info).build_payload(synergy_map)
    return payload, f"snapshot_resources={len(resources)} mapped={len(synergy_map)}"


def _compare_synergy_for_hero(champ_id: str, source_payload: dict, local_payload: dict) -> tuple[bool, list[str], dict]:
    src_items = (source_payload.get(champ_id, {}) or {}).get("synergy_items") or []
    local_items = (local_payload.get(champ_id, {}) or {}).get("synergy_items") or []
    src_keys = [_synergy_item_key(item) for item in src_items]
    local_keys = [_synergy_item_key(item) for item in local_items]
    issues = []
    if len(src_keys) != len(local_keys):
        issues.append(f"联动条数不一致：source={len(src_keys)} local={len(local_keys)}")
    missing = [item for item in src_keys if item not in local_keys]
    extra = [item for item in local_keys if item not in src_keys]
    if missing:
        issues.append(f"联动缺失：{missing[:3]}")
    if extra:
        issues.append(f"联动多出：{extra[:3]}")
    return not issues, issues, {"source_synergy": len(src_keys), "local_synergy": len(local_keys)}


def _champion_list_entry_map() -> dict[str, dict]:
    result = {}
    for item in load_precomputed_champion_list():
        if not isinstance(item, dict):
            continue
        champ_id = str(item.get("英雄 ID") or item.get("英雄ID") or "").strip()
        if champ_id:
            result[champ_id] = item
    return result


def run(args) -> tuple[int, dict]:
    core_data = load_champion_core_data()
    if not core_data:
        raise RuntimeError("Champion_Core_Data.json 读取失败")

    seed, hero_ids = _select_heroes(args, core_data)
    session = get_advanced_session()
    aug_data = _fetch_json_from_candidates(session, list(HEXTECH_AUGMENT_METADATA_URLS), dict)
    stats_list = _fetch_json_from_candidates(session, list(HEXTECH_CHAMPION_STATS_URLS), list)
    stats_by_id = {str(item.get("championId")): item for item in stats_list if isinstance(item, dict)}
    truth_dict = hex_scraper.load_augment_map()
    aug_id_map, aug_tier_map = _build_augment_maps(aug_data)

    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        raise RuntimeError("未找到最新 Hextech CSV")
    df = load_runtime_csv(latest_csv)

    api_client = _build_api_client()
    champion_list_map = _champion_list_entry_map()
    local_synergy_payload = _read_json(build_synergy_data_path()) if os.path.exists(build_synergy_data_path()) else {}
    source_synergy_payload = None
    synergy_source_status = "skipped"
    if args.include_synergy:
        source_synergy_payload, synergy_source_status = _load_snapshot_synergy_payload(core_data)

    report = {
        "seed": seed,
        "sample_size": len(hero_ids),
        "heroes": [],
        "latest_csv": latest_csv,
        "csv_hero_count": int(df["英雄名称"].nunique()) if "英雄名称" in df.columns else 0,
        "expected_hero_count": len(core_data),
        "synergy_source": synergy_source_status,
        "passed": True,
    }

    if report["csv_hero_count"] != report["expected_hero_count"]:
        report["passed"] = False
        report["global_issue"] = f"CSV 英雄数不一致：csv={report['csv_hero_count']} expected={report['expected_hero_count']}"

    if args.include_synergy and source_synergy_payload is None:
        report["passed"] = False
        report["synergy_blocker"] = synergy_source_status

    for champ_id in hero_ids:
        hero_info = core_data[champ_id]
        hero_name = str(hero_info.get("name") or champ_id)
        hero_report = {
            "hero_id": champ_id,
            "hero_name": hero_name,
            "title": hero_info.get("title", ""),
            "checks": {},
            "issues": [],
        }

        source_rows = _source_rows_for_hero(session, champ_id, core_data, stats_by_id, aug_id_map, truth_dict, aug_tier_map)
        csv_rows = df[df["英雄ID"].astype(str) == champ_id].copy() if "英雄ID" in df.columns else pd.DataFrame()
        csv_ok, csv_issues = _compare_csv_rows(source_rows, csv_rows)
        hero_report["checks"]["csv"] = {
            "passed": csv_ok,
            "source_count": len(source_rows),
            "csv_count": len(csv_rows),
        }
        hero_report["issues"].extend(csv_issues)

        champion_entry = champion_list_map.get(champ_id)
        if champion_entry is None:
            hero_report["issues"].append("Champion_List_Cache 缺失该英雄")
            hero_report["checks"]["champion_list_cache"] = {"passed": False}
        elif csv_rows.empty:
            hero_report["checks"]["champion_list_cache"] = {"passed": False}
        else:
            csv_first = csv_rows.iloc[0]
            list_ok = (
                _float_equal(champion_entry.get("英雄胜率"), csv_first.get("英雄胜率"))
                and _float_equal(champion_entry.get("英雄出场率"), csv_first.get("英雄出场率"))
            )
            hero_report["checks"]["champion_list_cache"] = {"passed": list_ok}
            if not list_ok:
                hero_report["issues"].append("Champion_List_Cache 英雄胜率/登场率与 CSV 不一致")

        cache_payload = load_precomputed_hextech_for_hero(hero_name) or {}
        cache_ok, cache_issues = _compare_payload_cards(source_rows, cache_payload, "Champion_Hextech_Cache")
        hero_report["checks"]["hextech_cache"] = {
            "passed": cache_ok,
            "cache_count": len(_cards_by_id(cache_payload)),
        }
        hero_report["issues"].extend(cache_issues)

        api_payload = _load_api_payload(api_client, hero_name)
        api_ok, api_issues = _compare_payload_cards(source_rows, api_payload, "local_api")
        hero_report["checks"]["api"] = {
            "passed": api_ok,
            "api_count": len(_cards_by_id(api_payload)),
        }
        hero_report["issues"].extend(api_issues)

        if args.include_synergy and source_synergy_payload is not None:
            synergy_ok, synergy_issues, synergy_counts = _compare_synergy_for_hero(
                champ_id,
                source_synergy_payload,
                local_synergy_payload if isinstance(local_synergy_payload, dict) else {},
            )
            hero_report["checks"]["synergy"] = {"passed": synergy_ok, **synergy_counts}
            hero_report["issues"].extend(synergy_issues)

        hero_passed = not hero_report["issues"] and all(
            check.get("passed", False) for check in hero_report["checks"].values()
        )
        hero_report["passed"] = hero_passed
        if not hero_passed:
            report["passed"] = False
        report["heroes"].append(hero_report)

    return 0 if report["passed"] else 1, report


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="验证本地海克斯/联动数据是否与源站完整对齐。")
    parser.add_argument("--sample-size", type=int, default=5, help="随机抽样英雄数量，默认 5。")
    parser.add_argument("--seed", type=int, default=None, help="随机 seed；缺省时使用当前时间戳并打印。")
    parser.add_argument("--include-synergy", action="store_true", help="同时校验 ApexLoL snapshot 联动数据。")
    parser.add_argument("--heroes", default="", help="指定英雄 ID/名称/别名，逗号或空格分隔；例如 25 或 莫甘娜。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code, report = run(args)
    except Exception as exc:
        report = {"passed": False, "error": f"{exc.__class__.__name__}: {exc}"}
        code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
