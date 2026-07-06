"""本地协同详情页校准工具。

该工具只访问本地 Hextech Web/API，用 Playwright 逐个打开详情页并报告右侧协同数据
的重复、疑似错配和 DOM/API 不一致；不访问 ApexLoL，不读取 cookie 或浏览器登录态。

调用方: 见 import 此模块的代码; 关键依赖: requests、catalog.runtime_store、scraping.version_sync。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.catalog.runtime_store import build_runtime_state_path, build_synergy_data_path  # noqa: E402
from hextech.scraping.version_sync import load_champion_core_data  # noqa: E402


COMMON_SHORT_ALIASES = {"q", "w", "e", "r", "ad", "ap", "aa"}


@dataclass
class Champion:
    id: str
    name: str
    title: str
    en_name: str
    aliases: list[str]


def _read_json(path: str | Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _normalize_text(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _load_core_champions() -> dict[str, Champion]:
    payload = load_champion_core_data()
    champions: dict[str, Champion] = {}
    for champ_id, item in payload.items():
        if not isinstance(item, dict):
            continue
        champions[str(champ_id)] = Champion(
            id=str(champ_id),
            name=str(item.get("name") or ""),
            title=str(item.get("title") or ""),
            en_name=str(item.get("en_name") or ""),
            aliases=[str(alias) for alias in item.get("aliases") or [] if str(alias).strip()],
        )
    return champions


def _hero_lookup(champions: dict[str, Champion]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for champ in champions.values():
        for value in [champ.id, champ.name, champ.title, champ.en_name, *champ.aliases]:
            key = _normalize_text(value)
            if key:
                lookup.setdefault(key, champ.id)
    return lookup


def _select_hero_ids(raw_heroes: str, champions: dict[str, Champion], limit: int) -> list[str]:
    if raw_heroes:
        lookup = _hero_lookup(champions)
        selected: list[str] = []
        for token in raw_heroes.replace(",", " ").split():
            champ_id = lookup.get(_normalize_text(token))
            if not champ_id:
                raise ValueError(f"无法解析英雄：{token}")
            if champ_id not in selected:
                selected.append(champ_id)
        return selected[:limit] if limit > 0 else selected
    ordered = sorted(champions.keys(), key=lambda item: int(item) if item.isdigit() else item)
    return ordered[:limit] if limit > 0 else ordered


def _items_signature(items: list[dict]) -> str:
    if not items:
        return ""
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_duplicate_index(synergy_payload: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for champ_id, item in synergy_payload.items():
        if not isinstance(item, dict):
            continue
        items = item.get("synergy_items") or []
        if not isinstance(items, list) or not items:
            continue
        signature = _items_signature(items)
        groups.setdefault(signature, []).append(str(champ_id))
    return {signature: ids for signature, ids in groups.items() if len(ids) > 1}


def _champion_terms(champ: Champion, *, include_short_chinese: bool = False) -> list[str]:
    terms: list[str] = []
    for value in [champ.name, champ.title, champ.en_name, *champ.aliases]:
        text = str(value or "").strip()
        norm = _normalize_text(text)
        if not norm or norm in COMMON_SHORT_ALIASES:
            continue
        if norm.isascii() and len(norm) < 3:
            continue
        if not norm.isascii() and len(norm) < 2 and not include_short_chinese:
            continue
        if text not in terms:
            terms.append(text)
    return terms


def _foreign_mentions(
    champ_id: str,
    items: list[dict],
    champions: dict[str, Champion],
    duplicate_with: list[str],
) -> list[dict]:
    text = _normalize_text(" ".join(str(item.get("content") or "") for item in items if isinstance(item, dict)))
    if not text:
        return []
    hits = []
    candidate_ids = duplicate_with or [item for item in champions if item != champ_id]
    for other_id in candidate_ids:
        other = champions.get(other_id)
        if other is None:
            continue
        if other_id == champ_id:
            continue
        matched = [
            term
            for term in _champion_terms(other, include_short_chinese=bool(duplicate_with))
            if _normalize_text(term) in text
        ]
        if matched:
            hits.append({"id": other_id, "name": other.name, "title": other.title, "terms": matched[:5]})
    return hits[:8]


def _api_synergy(base_url: str, champ_id: str) -> dict:
    response = requests.get(f"{base_url}/api/synergies/{quote(champ_id)}", timeout=15)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _dom_snapshot(page, base_url: str, champ: Champion, expected_items: int, timeout_ms: int) -> dict:
    url = f"{base_url}/detail.html?hero={quote(champ.name)}&id={quote(champ.id)}&en={quote(champ.en_name)}&calibration=1"
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if expected_items > 0:
        page.wait_for_function(
            "() => document.querySelectorAll('#synergyArticleScroll .hextech-article-inner').length > 0",
            timeout=timeout_ms,
        )
    else:
        page.wait_for_function(
            """
            () => {
              const container = document.querySelector('#synergyArticleScroll');
              if (!container || container.dataset.synergyLoaded !== '1') return false;
              const text = container ? container.textContent : '';
              return text.includes('暂无联动') || text.includes('该阶级无联动') || text.includes('待校准');
            }
            """,
            timeout=timeout_ms,
        )
    return page.evaluate(
        """
        () => {
          const skeleton = document.querySelector('#skeletonContainer');
          const container = document.querySelector('#synergyArticleScroll');
          const articles = Array.from(document.querySelectorAll('#synergyArticleScroll .hextech-article-inner'));
          return {
            skeletonDisplay: skeleton ? getComputedStyle(skeleton).display : '',
            articleCount: articles.length,
            textSample: container ? (container.innerText || '').slice(0, 240) : '',
            titles: articles.slice(0, 5).map(card => {
              const block = card.querySelector('.hextech-article-title-block');
              return block ? (block.innerText || '').split('\\n')[0] : '';
            }).filter(Boolean)
          };
        }
        """
    )


def _default_output_path() -> Path:
    reports_dir = Path(build_runtime_state_path("dummy")).parent.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"synergy_calibration_{time.strftime('%Y%m%d_%H%M%S')}.json"


def run_calibration(args) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - local dependency check
        raise RuntimeError("当前 Python 环境缺少 playwright，请使用已安装 Playwright 的解释器运行") from exc

    base_url = args.base_url.rstrip("/")
    champions = _load_core_champions()
    synergy_payload = _read_json(build_synergy_data_path(), {})
    duplicate_index = _build_duplicate_index(synergy_payload if isinstance(synergy_payload, dict) else {})
    duplicate_by_id = {
        champ_id: ids
        for ids in duplicate_index.values()
        for champ_id in ids
    }

    hero_ids = _select_hero_ids(args.heroes, champions, args.limit)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            for champ_id in hero_ids:
                champ = champions[champ_id]
                local_data = synergy_payload.get(champ_id, {}) if isinstance(synergy_payload, dict) else {}
                local_items = local_data.get("synergy_items") if isinstance(local_data, dict) else []
                local_items = local_items if isinstance(local_items, list) else []
                api_payload = _api_synergy(base_url, champ_id)
                api_items = api_payload.get("synergy_items") if isinstance(api_payload, dict) else []
                api_items = api_items if isinstance(api_items, list) else []
                api_status = str(api_payload.get("status") or "ok") if isinstance(api_payload, dict) else "ok"

                dom_error = ""
                dom = {"articleCount": 0, "skeletonDisplay": "", "titles": [], "textSample": ""}
                try:
                    dom = _dom_snapshot(page, base_url, champ, len(api_items), args.timeout_ms)
                except Exception as exc:  # pragma: no cover - browser timing is machine local
                    dom_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"

                duplicate_with = [item for item in duplicate_by_id.get(champ_id, []) if item != champ_id]
                warnings = []
                if duplicate_with:
                    warnings.append("duplicate_synergy_items")
                if dom_error:
                    warnings.append("dom_error")
                if int(dom.get("articleCount") or 0) != len(api_items):
                    warnings.append("dom_api_count_mismatch")
                foreign = _foreign_mentions(champ_id, local_items, champions, duplicate_with) if duplicate_with else []
                if foreign:
                    warnings.append("foreign_champion_terms")
                if api_status == "quarantined":
                    warnings.append("api_quarantined")

                row = {
                    "id": champ.id,
                    "name": champ.name,
                    "title": champ.title,
                    "en_name": champ.en_name,
                    "local_items": len(local_items),
                    "api_items": len(api_items),
                    "api_status": api_status,
                    "dom_articles": int(dom.get("articleCount") or 0),
                    "duplicate_with": duplicate_with,
                    "foreign_mentions": foreign,
                    "warnings": warnings,
                    "dom_titles": dom.get("titles") or [],
                    "dom_text_sample": dom.get("textSample") or "",
                    "dom_error": dom_error,
                }
                results.append(row)
                if args.pause_on_issue and warnings:
                    print(json.dumps(row, ensure_ascii=False, indent=2))
                    input("发现疑似问题，按 Enter 继续下一位英雄...")
        finally:
            browser.close()

    report = {
        "base_url": base_url,
        "synergy_file": build_synergy_data_path(),
        "checked": len(results),
        "issues": sum(1 for item in results if item["warnings"]),
        "duplicate_groups": list(duplicate_index.values()),
        "results": results,
    }
    output_path = Path(args.output).resolve() if args.output else _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["output"] = str(output_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Playwright 逐英雄校准本地详情页右侧协同数据。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="本地 Hextech Web 地址。")
    parser.add_argument("--heroes", default="", help="只校准指定英雄，支持 id/中文名/英文名，逗号或空格分隔。")
    parser.add_argument("--limit", type=int, default=0, help="最多校准多少个英雄；0 表示全部。")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于人工逐个查看。")
    parser.add_argument("--pause-on-issue", action="store_true", help="发现疑似问题时暂停等待人工确认。")
    parser.add_argument("--timeout-ms", type=int, default=20000, help="单页 DOM 等待超时。")
    parser.add_argument("--output", default="", help="校准报告输出路径；默认写入 data/runtime/reports。")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = run_calibration(args)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
