"""Crawl4AI 冒烟验证脚本。

独立运行，不依赖 run/scraping/ 任何代码。
用法：cd run && python -m crawler.smoke_crawl4ai
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys

# Windows 控制台默认 gbk，crawl4ai 输出含 unicode 字符会崩，提前切到 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def check_imports() -> None:
    """段 1：离线依赖检查，无需网络和浏览器。"""
    missing = []
    for pkg in ("crawl4ai", "playwright"):
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)

    if missing:
        print(f"[FAIL] 以下包未安装：{', '.join(missing)}")
        print("请执行：")
        print("  pip install -r run/crawler/requirements-crawl4ai.txt")
        print("  python -m playwright install chromium")
        sys.exit(1)

    print("[OK] crawl4ai + playwright 可 import")


async def smoke_fetch() -> None:
    """段 2：在线抓取冒烟，需要网络和已安装的 Chromium。"""
    from crawler.crawl4ai_client import fetch_page, _ensure_windows_event_loop

    _ensure_windows_event_loop()

    print("正在抓取 https://example.com ...")
    result = await fetch_page("https://example.com", output_format="markdown")

    if result.error:
        print(f"[FAIL] 抓取失败：{result.error}")
        sys.exit(1)

    if not result.markdown or "Example Domain" not in result.markdown:
        sample = json.dumps(str(result.markdown)[:200], ensure_ascii=False)
        print(f"[FAIL] 返回内容不符合预期，markdown 前 200 字：{sample}")
        sys.exit(1)

    print(f"[OK] 抓取成功，markdown 长度={len(result.markdown)}，status={result.status_code}")


if __name__ == "__main__":
    check_imports()
    asyncio.run(smoke_fetch())
