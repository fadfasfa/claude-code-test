"""ARAMMayhem Combos 普通 GET 抓取端。

本脚本只用 Scrapling ``Fetcher`` 的普通 HTTP GET 读取公开 combo 页面和
页面声明的 combo manifest，不启用 Stealthy、CloakBrowser、真实浏览器
profile、cookie 或代理池。输出固定保存 ARAMMayhem 原始字段和解析 rejects，
供后续清洗脚本合并到前端协同数据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from hextech.scraping.transport.scrapling_client import fetch_text
from hextech.support.atomic_io import atomic_write_json

DEFAULT_COMBO_URL = "https://arammayhem.com/zh-cn/combo/"
DEFAULT_OUTPUT = Path("data") / "raw" / "mayhem_combos.raw.json"
DEFAULT_TIMEOUT_MS = 30_000


def _configure_stdio() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _absolute_url(base_url: str, href: str) -> str:
    return urljoin(base_url, str(href or "").strip())


def _manifest_url_from_html(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    feed = soup.select_one("[data-combo-manifest-url]")
    if not feed:
        return ""
    return _absolute_url(base_url, feed.get("data-combo-manifest-url") or "")


def _rating_from_card(card: dict[str, Any]) -> str:
    return _text(card.get("tier") or card.get("rating"))


def _tier_from_card(card: dict[str, Any]) -> str:
    badges = card.get("typeBadges")
    if isinstance(badges, list) and badges:
        labels = [_text(item.get("label")) for item in badges if isinstance(item, dict)]
        labels = [item for item in labels if item]
        if labels:
            return ", ".join(labels)
    combo_ref = _text(card.get("comboRef"))
    if combo_ref.startswith("curated:"):
        return "Curated"
    return ""


def _card_to_item(card: dict[str, Any], base_url: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    champion = _text(card.get("champName") or card.get("championName") or card.get("championId"))
    champion_id = _text(card.get("championId"))
    augment_name = _text(card.get("augmentName"))
    augment_id = _text(card.get("augmentId"))
    body = _text(card.get("comboDescription") or card.get("description") or card.get("body"))
    href = _text(card.get("comboHref") or card.get("href"))
    source_url = _absolute_url(base_url, href) if href else base_url

    missing = []
    if not champion and not champion_id:
        missing.append("champion")
    if not augment_name and not augment_id:
        missing.append("augment")
    if not body:
        missing.append("body")
    if missing:
        return None, {
            "reason": "missing_required_fields",
            "missing": missing,
            "source_url": source_url,
            "raw_id": card.get("id"),
            "raw_slug": card.get("slug"),
        }

    return {
        "champion": champion,
        "champion_id": champion_id,
        "augment_names": [augment_name] if augment_name else [],
        "augment_id": augment_id,
        "mayhem_tier": _tier_from_card(card),
        "mayhem_rating": _rating_from_card(card),
        "body": body,
        "source_url": source_url,
    }, None


def _article_to_item(article, base_url: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    champion_id = _text(article.get("data-champion-id"))
    rating = _text(article.get("data-tier"))
    href_node = article.select_one('a[href*="/combo/"]')
    source_url = _absolute_url(base_url, href_node.get("href") if href_node else "")
    image = article.select_one("img[alt]")
    augment_name = _text(image.get("alt") if image else "")
    champion_node = article.select_one('a[href*="/build/"] span.font-medium')
    champion = _text(champion_node.get_text(" ") if champion_node else champion_id)
    body_node = article.select_one("p")
    body = _text(body_node.get_text(" ") if body_node else "")
    badges = [
        _text(node.get_text(" "))
        for node in article.select("span")
        if _text(node.get_text(" ")) and _text(node.get_text(" ")) not in {augment_name, rating}
    ]
    tier = badges[0] if badges else ""

    missing = []
    if not champion and not champion_id:
        missing.append("champion")
    if not augment_name:
        missing.append("augment")
    if not body:
        missing.append("body")
    if missing:
        return None, {
            "reason": "missing_required_fields",
            "missing": missing,
            "source_url": source_url or base_url,
            "raw_id": article.get("data-combo-id"),
        }

    return {
        "champion": champion,
        "champion_id": champion_id,
        "augment_names": [augment_name],
        "augment_id": "",
        "mayhem_tier": tier,
        "mayhem_rating": rating,
        "body": body,
        "source_url": source_url or base_url,
    }, None


def parse_combo_manifest(payload: dict[str, Any], base_url: str, *, max_pages: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cards = payload.get("cards")
    if not isinstance(cards, list):
        return [], [{"reason": "manifest_cards_missing", "source_url": base_url}], {"page_size": 0, "total": 0}

    page_size = int(payload.get("pageSize") or 0)
    total = int(payload.get("totalCombos") or len(cards))
    limit = len(cards)
    if max_pages > 0 and page_size > 0:
        limit = min(limit, page_size * max_pages)

    items: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    for card in cards[:limit]:
        if not isinstance(card, dict):
            rejects.append({"reason": "card_schema_mismatch", "source_url": base_url})
            continue
        item, reject = _card_to_item(card, base_url)
        if item:
            items.append(item)
        elif reject:
            rejects.append(reject)

    return items, rejects, {"page_size": page_size, "total": total, "selected": limit}


def parse_combo_html(html: str, base_url: str, *, max_pages: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    articles = soup.select("article.combo-card")
    if max_pages > 0:
        articles = articles[: 30 * max_pages]

    items: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    for article in articles:
        item, reject = _article_to_item(article, base_url)
        if item:
            items.append(item)
        elif reject:
            rejects.append(reject)
    return items, rejects, {"page_size": 30, "total": len(articles), "selected": len(articles)}


def scrape_mayhem_combos(
    *,
    url: str = DEFAULT_COMBO_URL,
    max_pages: int = 0,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    page_result = fetch_text(url, timeout_ms=timeout_ms)
    fetched_at = datetime.now(timezone.utc).isoformat()
    rejects: list[dict[str, Any]] = []
    if page_result.error or page_result.status_code != 200 or not page_result.text:
        return {
            "schema_version": 1,
            "source": "arammayhem",
            "source_url": url,
            "fetched_at": fetched_at,
            "items": [],
            "rejects": [{
                "reason": "combo_page_fetch_failed",
                "status_code": page_result.status_code,
                "error": page_result.error,
                "source_url": url,
            }],
        }

    manifest_url = _manifest_url_from_html(page_result.text, url)
    items: list[dict[str, Any]] = []
    parse_meta: dict[str, Any] = {}
    if manifest_url:
        manifest_result = fetch_text(manifest_url, timeout_ms=timeout_ms)
        if not manifest_result.error and manifest_result.status_code == 200 and manifest_result.text:
            try:
                manifest_payload = json.loads(manifest_result.text)
                if isinstance(manifest_payload, dict):
                    items, rejects, parse_meta = parse_combo_manifest(
                        manifest_payload,
                        url,
                        max_pages=max_pages,
                    )
                else:
                    rejects.append({"reason": "manifest_schema_mismatch", "source_url": manifest_url})
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                rejects.append({"reason": "manifest_json_decode_failed", "error": str(exc), "source_url": manifest_url})
        else:
            rejects.append({
                "reason": "manifest_fetch_failed",
                "status_code": manifest_result.status_code,
                "error": manifest_result.error,
                "source_url": manifest_url,
            })

    if not items:
        items, html_rejects, parse_meta = parse_combo_html(page_result.text, url, max_pages=max_pages)
        rejects.extend(html_rejects)

    return {
        "schema_version": 1,
        "source": "arammayhem",
        "source_url": url,
        "manifest_url": manifest_url,
        "fetched_at": fetched_at,
        "max_pages": max_pages,
        "page": parse_meta,
        "items": items,
        "rejects": rejects,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取 ARAMMayhem Combos 原始数据。")
    parser.add_argument("--url", default=DEFAULT_COMBO_URL, help="Combos 页面 URL。")
    parser.add_argument("--max-pages", type=int, default=0, help="最多抓取的页面数；0 表示读取 manifest 全量。")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="单次 HTTP GET 超时毫秒数。")
    parser.add_argument("--output", default=os.fspath(DEFAULT_OUTPUT), help="输出 raw JSON 路径。")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    payload = scrape_mayhem_combos(
        url=args.url,
        max_pages=max(0, int(args.max_pages or 0)),
        timeout_ms=int(args.timeout_ms or DEFAULT_TIMEOUT_MS),
    )
    atomic_write_json(args.output, payload, ensure_ascii=False, indent=2)
    print(json.dumps({
        "output": args.output,
        "items": len(payload.get("items") or []),
        "rejects": len(payload.get("rejects") or []),
        "source_url": payload.get("source_url"),
        "manifest_url": payload.get("manifest_url", ""),
    }, ensure_ascii=False, indent=2))
    return 0 if payload.get("items") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
