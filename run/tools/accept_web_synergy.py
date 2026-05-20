"""Web/UI 详情页联动验收脚本。

用途：
- 用固定 seed 从本地 ``/api/champions`` 随机抽取英雄。
- 用真实浏览器打开本地详情页，并对照 ApexLoL 源站详情页。
- 验收右侧联动名称、阶级、评分、标签、正文片段和阶级筛选。

该脚本是人工验收入口，不放进默认 unittest，避免普通测试依赖外网和浏览器。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from scraping.full_synergy_scraper import (
    ApexSource,
    SynergyEntry,
    SynergyExtractor,
    _load_json_file,
    build_augment_name_map_from_static,
    build_champion_lookup,
    build_core_info,
)


TIER_IDS = ("Prismatic", "Gold", "Silver")


def _normalize_text(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _normalize_tier(value: Any) -> str:
    text = str(value or "").strip()
    if "棱彩" in text or "彩色" in text or text == "Prismatic":
        return "Prismatic"
    if "黄金" in text or "金" in text or text == "Gold":
        return "Gold"
    if "白银" in text or "银" in text or text == "Silver":
        return "Silver"
    return text


def _entry_to_expected(entry: SynergyEntry) -> dict:
    return {
        "name": ", ".join(entry.augment_names),
        "names": list(entry.augment_names),
        "tier": entry.tier,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "content": entry.content,
    }


def _make_local_driver():
    errors = []
    headless = os.getenv("ACCEPT_HEADLESS", "1").strip() != "0"
    browser = os.getenv("ACCEPT_BROWSER", "auto").strip().lower() or "auto"
    if browser in {"auto", "edge"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EdgeOptions

            options = EdgeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Edge(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"edge={exc.__class__.__name__}:{str(exc)[:120]}")
    if browser in {"auto", "chrome"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions

            options = ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"chrome={exc.__class__.__name__}:{str(exc)[:120]}")
    raise RuntimeError("无法启动验收浏览器：" + ", ".join(errors))


def _safe_get(driver, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        # 源站广告/长连接偶尔拖住 load；后续用 DOM 轮询判断页面是否可用。
        pass


def _wait_local_cards(driver, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const container = document.querySelector('#synergyArticleScroll');
            return {
              cards: document.querySelectorAll('#synergyArticleScroll .hextech-card').length,
              text: container ? container.textContent : ''
            };
            """
        )
        text = state.get("text", "")
        if state.get("cards", 0) > 0 or "暂无联动" in text or "该阶级无联动" in text:
            return
        time.sleep(0.5)
    raise TimeoutError("本地详情页联动区域等待超时")


def _extract_local_cards(driver) -> list[dict]:
    return driver.execute_script(
        """
        return Array.from(document.querySelectorAll('#synergyArticleScroll .hextech-card')).map(card => {
          const titleBlock = card.querySelector('.hextech-article-title-block');
          const row = titleBlock && titleBlock.children ? titleBlock.children[0] : null;
          const spans = Array.from(row ? row.querySelectorAll('span') : [])
            .map(s => (s.textContent || '').trim()).filter(Boolean);
          const title = spans[0] || '';
          const rating = spans[1] || '';
          const tier = titleBlock && titleBlock.children && titleBlock.children[1]
            ? (titleBlock.children[1].textContent || '').trim()
            : '';
          const content = (card.querySelector('.hextech-article-content')?.textContent || '').trim();
          const headTexts = Array.from(row ? row.children : [])
            .map(el => (el.textContent || '').trim()).filter(Boolean);
          const tags = headTexts.filter(text => (
            text && text !== title && text !== rating && text !== '?' && !text.includes(`${rating}\\n`)
          ));
          const img = card.querySelector('img[data-hextech-name]');
          return {
            title,
            rating,
            tier,
            tags,
            content,
            resolvedName: img ? (img.getAttribute('data-hextech-name') || '') : '',
            resolvedTier: img ? (img.getAttribute('data-hextech-tier') || '') : ''
          };
        });
        """
    )


def _click_tier(driver, tier: str) -> None:
    driver.execute_script(
        """
        const tier = arguments[0];
        const button = document.getElementById(`tab-${tier}`);
        if (!button) throw new Error(`missing tier button ${tier}`);
        button.click();
        """,
        tier,
    )
    _wait_local_cards(driver)


def _compare_expected_to_local(expected: dict, local: dict) -> dict:
    tag_ok = not expected["tag"] or any(
        _normalize_text(expected["tag"]) in _normalize_text(tag)
        for tag in local.get("tags", [])
    )
    possible_names = {_normalize_text(name) for name in expected["names"]}
    resolved_name = _normalize_text(local.get("resolvedName") or local.get("title"))
    return {
        "name": _normalize_text(local.get("title")) == _normalize_text(expected["name"]),
        "tier": _normalize_tier(local.get("tier")) == _normalize_tier(expected["tier"]),
        "rating": _normalize_text(local.get("rating")) == _normalize_text(expected["rating"]),
        "tag": tag_ok,
        "content": _normalize_text(local.get("content")).find(_normalize_text(expected["content"])[:24]) >= 0,
        "catalog": resolved_name in possible_names,
    }


def _source_visible_matches(driver, expected: list[dict]) -> list[dict]:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    clean_text = _normalize_text(text)
    results = []
    for item in expected:
        results.append(
            {
                "name": all(_normalize_text(name) in clean_text for name in item["names"]),
                "tier": _normalize_text(item["tier"]) in clean_text,
                "rating": _normalize_text(item["rating"]) in clean_text,
                "tag": _normalize_text(item["tag"]) in clean_text,
                "content": _normalize_text(item["content"])[:24] in clean_text,
            }
        )
    return results


def _source_page_blocked(driver) -> bool:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    normalized = _normalize_text(text)
    return (
        "deploymentpaused" in normalized
        or "service_unavailable" in normalized
        or "503" in normalized
    )


def _all_match(rows: list[dict]) -> bool:
    return bool(rows) and all(all(row.values()) for row in rows)


def _wait_source_visible(driver, source_url: str, expected: list[dict]) -> list[dict]:
    """等待源站详情页真正把右侧联动文本渲染出来。

    ApexLoL 偶尔会先返回页面骨架或局部文本；验收脚本如果只看第一次
    navigation，容易把源站未完成渲染误判成本地错位。这里保留真实浏览器
    口径，但对同一 URL 做短重试，只有字段仍然对不上才记录失败截图。
    """

    last_matches: list[dict] = []
    for attempt in range(3):
        _safe_get(driver, source_url)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if _source_page_blocked(driver):
                break
            last_matches = _source_visible_matches(driver, expected)
            if _all_match(last_matches):
                return last_matches
            time.sleep(0.75)
        if attempt < 2:
            time.sleep(2)
    return last_matches


def _resolve_base_url(args) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    if args.port_file:
        port = Path(args.port_file).read_text(encoding="utf-8").strip()
        return f"http://127.0.0.1:{port}"
    return "http://127.0.0.1:8000"


def _load_local_champions(base_url: str) -> list[dict]:
    response = requests.get(f"{base_url}/api/champions", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{base_url}/api/champions 未返回英雄列表")
    return payload


def _build_source_extractor() -> SynergyExtractor:
    core_info = build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
    return SynergyExtractor(
        champion_lookup=build_champion_lookup(core_info),
        augment_name_map=build_augment_name_map_from_static(),
    )


def run_acceptance(args) -> dict:
    base_url = _resolve_base_url(args)
    source_base = args.source_base.rstrip("/")
    champions = _load_local_champions(base_url)
    rng = random.Random(args.seed)
    shuffled = list(champions)
    rng.shuffle(shuffled)

    source = ApexSource()
    extractor = _build_source_extractor()
    local_driver = _make_local_driver()
    screenshots = Path(args.screenshot_dir).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    selected = []
    attempts = []

    try:
        for champion in shuffled:
            champ_id = str(champion.get("英雄 ID") or champion.get("英雄ID") or "").strip()
            hero_name = str(champion.get("英雄名称") or "").strip()
            en_name = str(champion.get("英文名") or "").strip()
            if not champ_id or not hero_name or not en_name:
                continue

            source_url = f"{source_base}/champions/{en_name}"
            resource = source.fetch(source_url, allow_browser=True)
            entries = extractor._extract_from_resource(resource) if resource else []
            attempts.append({"id": champ_id, "name": hero_name, "en": en_name, "source_entries": len(entries)})
            if not entries:
                continue

            expected = [_entry_to_expected(entry) for entry in entries[: args.first_n]]
            local_url = (
                f"{base_url}/detail.html?hero={quote(hero_name)}&id={quote(champ_id)}&en={quote(en_name)}"
                f"&acceptance={args.seed}"
            )
            _safe_get(local_driver, local_url)
            _wait_local_cards(local_driver)
            _click_tier(local_driver, "all")
            local_cards = _extract_local_cards(local_driver)[: len(expected)]
            comparisons = [
                _compare_expected_to_local(item, local_cards[index] if index < len(local_cards) else {})
                for index, item in enumerate(expected)
            ]

            tier_checks = []
            for tier in TIER_IDS:
                _click_tier(local_driver, tier)
                cards = _extract_local_cards(local_driver)
                violations = [
                    {"title": card.get("title", ""), "tier": card.get("tier", ""), "resolvedTier": card.get("resolvedTier", "")}
                    for card in cards
                    if _normalize_tier(card.get("tier") or card.get("resolvedTier")) != tier
                ]
                tier_checks.append({"tier": tier, "count": len(cards), "violations": violations})

            source_matches = _wait_source_visible(local_driver, source_url, expected)

            result = {
                "id": champ_id,
                "name": hero_name,
                "en": en_name,
                "local_url": local_url,
                "source_url": source_url,
                "source_entries": len(entries),
                "local_count": len(local_cards),
                "field_ok": len(local_cards) == len(expected) and _all_match(comparisons),
                "tier_ok": all(not check["violations"] for check in tier_checks),
                "source_visible_ok": _all_match(source_matches),
                "comparisons": comparisons,
                "tier_checks": tier_checks,
                "source_matches": source_matches,
            }
            if not (result["field_ok"] and result["tier_ok"] and result["source_visible_ok"]):
                screenshot = screenshots / f"{args.label}-{champ_id}-{en_name}.png"
                try:
                    local_driver.save_screenshot(str(screenshot))
                    result["screenshot"] = str(screenshot)
                except Exception as exc:
                    result["screenshot_error"] = exc.__class__.__name__
            selected.append(result)
            if len(selected) >= args.sample_size:
                break
    finally:
        source.close()
        try:
            local_driver.quit()
        except Exception:
            pass

    passed = len(selected) == args.sample_size and all(
        item["field_ok"] and item["tier_ok"] and item["source_visible_ok"]
        for item in selected
    )
    return {
        "label": args.label,
        "base_url": base_url,
        "source_base": source_base,
        "seed": args.seed,
        "sample_size": args.sample_size,
        "first_n": args.first_n,
        "passed": passed,
        "selected": selected,
        "attempts": attempts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收 Web/UI 详情页右侧联动是否对齐 ApexLoL 源页。")
    parser.add_argument("--base-url", default="", help="本地 Web 地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--port-file", default="", help="UI/Web 写出的 web_server_port.txt；未传 base-url 时使用")
    parser.add_argument("--source-base", default="https://apexlol.info/zh")
    parser.add_argument("--seed", type=int, default=20260518)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--first-n", type=int, default=5)
    parser.add_argument("--label", default="web", help="输出标签，例如 web 或 ui")
    parser.add_argument("--screenshot-dir", default=os.path.join("data", "runtime", "acceptance", "synergy"))
    args = parser.parse_args(argv)

    result = run_acceptance(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
