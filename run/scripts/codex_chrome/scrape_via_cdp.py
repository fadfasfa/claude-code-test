from __future__ import annotations

"""通过现有 Chrome CDP 会话重抓 ApexLoL hero synergy。

脚本只连接 `http://127.0.0.1:9222` 上已经运行的 fresh Chrome，并复用
`browser.contexts[0]`，避免丢失用户手动通过 Cloudflare 后留在 profile 中的 cookie。
输出为每个 hero 一个 JSON 文件；文件写出后立即执行 strict-pass 校验。
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
TARGETS_PATH = SCRIPT_DIR / "targets_to_rescrape.json"
DELTA_DIR = REPO_ROOT / "run" / "data" / "raw" / "synergy" / "codex_goal_delta"
CDP_ENDPOINT = "http://127.0.0.1:9222"
CARD_SELECTOR = ".interaction-card-shell"
WAIT_MS = 20_000

RATING_RE = re.compile(r"^(SSS|SS|S|A|B|C|D)\s*(?:级|Tier|评分)?", re.IGNORECASE)
COUNT_RE = re.compile(r"(\d+)")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _filename_for(target: dict[str, Any]) -> Path:
    return DELTA_DIR / f"{target['id']}_{target['en_name']}.json"


def _live_url(raw_url: str) -> str:
    """ApexLoL 非 www 域会对 fresh Chrome 返回 403；页面真源使用 www 域。"""
    return str(raw_url or "").replace("https://apexlol.info/", "https://www.apexlol.info/")


def _load_targets() -> list[dict[str, Any]]:
    payload = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    targets = payload.get("targets") if isinstance(payload, dict) else payload
    if not isinstance(targets, list) or len(targets) != 76:
        raise ValueError(f"targets_to_rescrape.json 目标数量异常: {len(targets) if isinstance(targets, list) else '非列表'}")
    return targets


def _validate_hero(hero: dict[str, Any]) -> tuple[bool, str]:
    items = hero.get("synergy_items")
    if not isinstance(items, list):
        return False, "synergy_items_not_list"
    declared = hero.get("declared_count")
    actual = hero.get("actual_card_count")
    if declared != actual or actual != len(items):
        return False, f"count_mismatch declared={declared} actual={actual} len={len(items)}"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"card_{index}_not_object"
        missing = []
        for key in ("author", "content", "augment_names", "tier"):
            value = item.get(key)
            if isinstance(value, list):
                ok = any(_clean_text(part) for part in value)
            else:
                ok = bool(_clean_text(value))
            if not ok:
                missing.append(key)
        for key in ("upvotes", "downvotes"):
            if key not in item or item.get(key) is None:
                missing.append(key)
        if missing:
            return False, f"card_{index}_missing_required_fields:{','.join(missing)}"
    return True, ""


def _strict_pass_file(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    ok, _reason = _validate_hero(payload)
    return ok


async def _extract_page(page, target: dict[str, Any]) -> dict[str, Any]:
    return await page.evaluate(
        """
        ({ target, cardSelector }) => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const linesOf = element => clean(element ? element.innerText || element.textContent || '' : '')
            .split(/\\n|\\r/)
            .map(clean)
            .filter(Boolean);
          const intText = value => {
            const match = clean(value).match(/-?\\d+/);
            return match ? Number.parseInt(match[0], 10) : 0;
          };
          const normalizeTier = value => {
            const text = clean(value).replace(/阶$/, '');
            if (!text) return '';
            if (/棱彩|Prismatic/i.test(text)) return '棱彩';
            if (/黄金|Gold/i.test(text)) return '黄金';
            if (/白银|Silver/i.test(text)) return '白银';
            return text;
          };
          const firstText = (root, selectors) => {
            for (const selector of selectors) {
              const node = root.querySelector(selector);
              const text = clean(node ? node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') : '');
              if (text) return text;
            }
            return '';
          };
          const unique = values => Array.from(new Set(values.map(clean).filter(Boolean)));
          const cards = Array.from(document.querySelectorAll(cardSelector));
          const synergyItems = cards.map((card, index) => {
            const rawText = clean(card.innerText || card.textContent || '');
            const lines = linesOf(card);
            const augmentDetails = [];
            const augmentNodes = Array.from(card.querySelectorAll('a[href*="/hextech/"], [href*="/hextech/"]'));
            for (const node of augmentNodes) {
              const href = node.getAttribute('href') || '';
              const text = clean(node.innerText || node.textContent || node.getAttribute('title') || node.getAttribute('aria-label'));
              if (!href && !text) continue;
              const tierNearby = clean(
                node.closest('[class*="hextech"], [class*="augment"], [class*="interaction"]')?.innerText || ''
              );
              const tierLine = (tierNearby.split(/\\n|\\r/).map(clean).find(item => /棱彩|黄金|白银|Prismatic|Gold|Silver/.test(item)) || '');
              augmentDetails.push({
                href,
                name: text.split(/\\n|\\r/).map(clean).filter(Boolean)[0] || text,
                rich_id: href.split('/').filter(Boolean).pop() || '',
                tier: normalizeTier(tierLine),
              });
            }
            let augmentNames = unique(augmentDetails.map(item => item.name));
            let tier = normalizeTier(firstText(card, ['[class*="tier"]', '[class*="rarity"]']));
            if (!tier) {
              tier = normalizeTier(lines.find(line => /棱彩|黄金|白银|Prismatic|Gold|Silver/.test(line)) || '');
            }
            const ratingLine = lines.find(line => /^(SSS|SS|S|A|B|C|D)\\s*(级|Tier|评分)?/i.test(line)) || '';
            const ratingMatch = ratingLine.match(/^(SSS|SS|S|A|B|C|D)/i);
            const rating = ratingMatch ? ratingMatch[1].toUpperCase() : firstText(card, ['[class*="rating"]', '[class*="grade"]']);
            const tag = (
              lines.find(line => ['强力联动', '娱乐', '陷阱', '缺陷'].includes(line)) ||
              firstText(card, ['[class*="tag"]', '[class*="badge"]']) ||
              ''
            );
            const isOriginal = lines.includes('原创') || /\\bOriginal\\b/i.test(rawText);
            let author = '';
            const authorLabelIndex = lines.findIndex(line => line === '作者' || /^作者[:：]/.test(line));
            if (authorLabelIndex >= 0) {
              const label = lines[authorLabelIndex];
              author = label.replace(/^作者[:：]/, '').trim();
              if (!author) author = lines[authorLabelIndex + 1] || '';
            }
            if (!author) {
              for (const label of Array.from(card.querySelectorAll('span, div'))) {
                if (clean(label.innerText || label.textContent) !== '作者') continue;
                const next = label.nextElementSibling;
                author = clean(next ? next.innerText || next.textContent : '');
                if (author) break;
              }
            }
            if (!author) author = firstText(card, ['[class*="author"]', '[class*="user"]']);
            const voteCandidates = [];
            for (const line of lines) {
              if (/^-?\\d+$/.test(line)) voteCandidates.push(Number.parseInt(line, 10));
            }
            let content = firstText(card, ['.interaction-note', '[class*="content"]', '[class*="description"]', '[class*="comment"]', 'p']);
            if (!content) {
              const stop = new Set(['推荐出装', '关联 1 个海克斯', '关联 2 个海克斯', '关联 3 个海克斯', '关联 4 个海克斯']);
              const start = authorLabelIndex >= 0 ? authorLabelIndex + (author && lines[authorLabelIndex] !== author ? 2 : 1) : -1;
              const contentLines = [];
              for (let i = Math.max(0, start); i < lines.length; i += 1) {
                const line = lines[i];
                if (!line || line === author || stop.has(line) || /^\\+\\d+$/.test(line) || /^\\d{4}年/.test(line)) break;
                if (/^-?\\d+$/.test(line) || ['原创', '非原创', '强力联动', '娱乐', '陷阱', '缺陷'].includes(line)) continue;
                if (/^(SSS|SS|S|A|B|C|D)\\s*(级|Tier|评分)?/i.test(line)) continue;
                if (/棱彩|黄金|白银|Prismatic|Gold|Silver/.test(line)) continue;
                if (augmentNames.includes(line)) continue;
                contentLines.push(line);
              }
              content = clean(contentLines.join(' '));
            }
            if (!augmentNames.length) {
              const ratingIndex = lines.indexOf(ratingLine);
              for (let i = Math.max(0, ratingIndex - 4); i < ratingIndex; i += 1) {
                const line = lines[i];
                if (line && !/关联|棱彩|黄金|白银|Prismatic|Gold|Silver/.test(line)) augmentNames.push(line);
              }
              augmentNames = unique(augmentNames);
            }
            return {
              augment_details: augmentDetails,
              augment_names: augmentNames,
              tier,
              rating,
              tag,
              author: clean(author),
              is_original: isOriginal,
              content,
              upvotes: voteCandidates.length > 0 ? voteCandidates[0] : 0,
              downvotes: voteCandidates.length > 1 ? voteCandidates[1] : 0,
              raw_text: rawText,
              index,
            };
          });
          return {
            id: String(target.id),
            name: target.name || '',
            title: target.title || '',
            en_name: target.en_name || '',
            source_url: target.source_url || location.href,
            final_url: location.href,
            declared_count: synergyItems.length,
            actual_card_count: synergyItems.length,
            synergy_items: synergyItems,
          };
        }
        """,
        {"target": target, "cardSelector": CARD_SELECTOR},
    )


async def _wait_for_cards_or_challenge(page) -> tuple[bool, str]:
    try:
        await page.wait_for_selector(CARD_SELECTOR, timeout=WAIT_MS)
        return True, ""
    except Exception:
        text = _clean_text(await page.locator("body").inner_text(timeout=5_000))
        challenge_markers = (
            "Checking your browser",
            "验证您是否是真人",
            "请稍候",
            "cf-challenge",
            "Cloudflare",
            "Access Denied",
        )
        if any(marker.lower() in text.lower() for marker in challenge_markers):
            print(f"CLOUDFLARE_CHALLENGE {page.url}", flush=True)
            for _ in range(1):
                await page.wait_for_timeout(3_000)
                await page.reload(wait_until="domcontentloaded", timeout=WAIT_MS)
                try:
                    await page.wait_for_selector(CARD_SELECTOR, timeout=10_000)
                    return True, ""
                except Exception:
                    continue
            return False, "cloudflare_challenge_not_cleared"
        return False, f"cards_not_rendered body={text[:120]}"


async def _scrape_one(context, target: dict[str, Any]) -> tuple[bool, str]:
    output_path = _filename_for(target)
    if _strict_pass_file(output_path):
        print(f"SKIP strict-pass {output_path.name}", flush=True)
        return True, ""

    page = await context.new_page()
    try:
        for attempt in range(1, 2):
            await page.goto(_live_url(target["source_url"]), wait_until="domcontentloaded", timeout=WAIT_MS)
            rendered, render_reason = await _wait_for_cards_or_challenge(page)
            if not rendered:
                return False, render_reason
            hero = await _extract_page(page, target)
            ok, reason = _validate_hero(hero)
            if ok:
                output_path.write_text(json.dumps(hero, ensure_ascii=False, indent=2), encoding="utf-8")
                ok_after_write, written_reason = _validate_hero(json.loads(output_path.read_text(encoding="utf-8")))
                if not ok_after_write:
                    return False, written_reason
                print(f"PASS {target['id']} {target['en_name']} cards={hero['actual_card_count']}", flush=True)
                await page.wait_for_timeout(5000)
                return True, ""
            if attempt < 1:
                print(f"RETRY {target['id']} {target['en_name']} attempt={attempt} reason={reason}", flush=True)
                await page.reload(wait_until="domcontentloaded", timeout=WAIT_MS)
                await page.wait_for_timeout(1500)
                continue
            return False, reason
    finally:
        await page.close()


async def main() -> int:
    targets = _load_targets()
    DELTA_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, str]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        try:
            if not browser.contexts:
                raise RuntimeError("CDP Chrome 没有可复用 browser.contexts[0]")
            context = browser.contexts[0]
            for index, target in enumerate(targets, start=1):
                ok, reason = await _scrape_one(context, target)
                if not ok:
                    failures.append({"id": str(target["id"]), "en_name": str(target["en_name"]), "reason": reason})
                    print(f"FAIL {target['id']} {target['en_name']} reason={reason}", flush=True)
                print(f"PROGRESS {index}/{len(targets)} failures={len(failures)}", flush=True)
        finally:
            await browser.close()

    files = list(DELTA_DIR.glob("*.json"))
    strict_pass = sum(1 for path in files if _strict_pass_file(path))
    summary = {
        "scrape_via_cdp": str(Path(__file__).resolve()),
        "delta_dir": str(DELTA_DIR),
        "delta_file_count": len(files),
        "strict_pass_count": strict_pass,
        "failures": failures,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if len(files) == 76 and strict_pass == 76 and not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
