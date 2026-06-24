# Codex Chrome 扩展任务：ApexLoL 76 英雄严格重新抓取

<!--
  中文说明：
  本文件是给 Codex AI 的任务指令模板，用于通过 CDP 连接到已运行的 Chrome 实例，
  抓取 76 个英雄的协同数据。执行入口是 launch_codex_chrome.ps1，验证用
  verify_codex_chrome.ps1，完成后用 cleanup.ps1 清理临时产物。
  正文保留英文以确保 Codex 执行准确性。
-->

Use the fresh Chrome profile that is already running with CDP at `http://127.0.0.1:9222`.
Do not use stale browser/search/web fallback content. Use the live rendered Chrome pages only.

## Execution Mechanism (mandatory)

Your built-in browser runtime (`node_repl` browser-client, "Browser Use", any
internal Chrome control surface) fails on this machine with
`windows sandbox cap_sid read failure`. Any attempt to use it dies before it
ever reaches Chrome. You must therefore not use it at all for this task.

Required mechanism instead:

1. Write a Python scraper at `run/scripts/codex_chrome/scrape_via_cdp.py`.
2. Inside the script, use `playwright.async_api` and call
   `playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")` to attach to
   the already-running fresh Chrome. Do NOT launch a new Chromium.
3. Reuse the existing browser context with `browser.contexts[0]` and open each
   target URL via `context.new_page()`. Do NOT create a new context — a new
   context would lose the Cloudflare cookies the user already cleared.
4. Run the script as a normal subprocess (e.g.
   `python run/scripts/codex_chrome/scrape_via_cdp.py`). File I/O and
   subprocess spawning are NOT affected by the cap_sid bug; only your built-in
   browser runtime is.
5. The script iterates `run/scripts/codex_chrome/targets_to_rescrape.json`,
   opens each `source_url`, waits for `.interaction-card-shell` to render,
   extracts cards, writes one JSON per hero under
   `run/data/raw/synergy/codex_goal_delta/`, and validates the Strict Pass
   Contract (see below) per file before moving on.
6. If a hero hits the Cloudflare challenge page, the script must pause and
   print the URL. Do NOT mark it passed. The user clears the challenge once in
   the fresh Chrome window, then the script retries that URL.
7. If the `playwright` Python package is missing, run `pip install playwright`.
   Do NOT run `playwright install` — we attach to the existing Chrome, we do
   not need bundled Chromium binaries.
8. Be idempotent: if `run/data/raw/synergy/codex_goal_delta/<id>_<en>.json`
   already exists and already satisfies the Strict Pass Contract, skip it.

## Pre-conditions

- `http://127.0.0.1:9222/json/version` returns Chrome 148+. Verify with
  `run/scripts/codex_chrome/verify_codex_chrome.ps1` before scraping.
- The user has manually passed Cloudflare on at least one apexlol champion
  page in the fresh Chrome window so the profile holds valid cf_clearance
  cookies that all later tabs will share.
- The `playwright` Python package is importable in the environment that runs
  the script.

## Inputs

Read targets from:

`run/scripts/codex_chrome/targets_to_rescrape.json`

The target list has 76 heroes. For each target, use `source_url` as the live page URL.

## Output Directory

Write one JSON file per hero to:

`run/data/raw/synergy/codex_goal_delta/`

Filename format:

`<id>_<en_name>.json`

## Output Schema

Each file must be a single hero subtree compatible with `Champion_Synergy_20260519_223505.json` and must include at least:

```json
{
  "id": "5",
  "name": "德邦总管",
  "title": "赵信",
  "en_name": "XinZhao",
  "declared_count": 8,
  "actual_card_count": 8,
  "synergy_items": [
    {
      "augment_names": ["..."],
      "tier": "黄金|棱彩|白银|...",
      "rating": "SS|S|A|...",
      "tag": "强力联动|娱乐|...",
      "author": "...",
      "is_original": true,
      "content": "...",
      "upvotes": 0,
      "downvotes": 0
    }
  ]
}
```

## Strict Pass Contract

A hero is acceptable only if:

- `declared_count == actual_card_count == len(synergy_items)`
- every card has non-empty `author`, `content`, `augment_names`, and `tier`
- every card contains `upvotes` and `downvotes`; `0` is valid, `null` or missing is invalid
- do not fabricate missing fields; if the rendered page does not expose a field, retry/reload/inspect the rendered DOM until the strict contract is met

After each hero file is written, validate that file against the strict pass contract before moving to the next hero.

## Finish Condition

All 76 files must exist under `run/data/raw/synergy/codex_goal_delta/` and each must pass the strict contract.

Do not modify `Champion_Synergy_20260519_223505.json`; that merge is handled by `run/scripts/merge_codex_goal_into_baseline.py` after this goal completes.
