"""Crawl4AI 异步抓取客户端。

供 CC/CX 调用，独立于 run/scraping/ 现有爬虫；输入 URL，输出 Markdown 或 HTML。
依赖：crawl4ai>=0.6.0，安装见 requirements-crawl4ai.txt。
"""

from __future__ import annotations

import sys
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass
class FetchResult:
    url: str
    markdown: str | None
    html: str | None
    status_code: int | None
    fetched_at: str   # ISO 8601 UTC
    error: str | None


def _require_crawl4ai() -> None:
    """crawl4ai 未安装时给出明确提示，而非抛裸 ImportError。"""
    try:
        import crawl4ai  # noqa: F401
    except ImportError:
        raise ImportError(
            "crawl4ai 未安装。请执行：\n"
            "  pip install -r run/crawler/requirements-crawl4ai.txt\n"
            "  python -m playwright install chromium"
        ) from None


async def fetch_page(
    url: str,
    *,
    output_format: Literal["markdown", "html", "both"] = "markdown",
    headless: bool = True,
    timeout_ms: int = 30_000,
    wait_for: str | None = None,
    js_code: str | None = None,
    proxy: str | None = None,
) -> FetchResult:
    """抓取单个页面，返回 Markdown 和/或 HTML。

    参数：
        url: 目标页面地址。
        output_format: 输出格式，"markdown"、"html" 或 "both"。
        headless: 是否无头模式，默认 True。
        timeout_ms: 页面加载超时毫秒数，默认 30000。
        wait_for: CSS selector，等待该元素出现后再截取内容。
        js_code: 页面加载后执行的 JS 字符串（可选）。
        proxy: 代理地址，调用方显式传入，不从环境变量读取。
    """
    _require_crawl4ai()

    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode  # type: ignore

    need_markdown = output_format in ("markdown", "both")
    need_html = output_format in ("html", "both")

    browser_config = BrowserConfig(
        headless=headless,
        proxy=proxy,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=timeout_ms,
        wait_for=wait_for,
        js_code=js_code,
    )

    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)

        return FetchResult(
            url=url,
            markdown=result.markdown.raw_markdown if need_markdown and result.markdown else None,
            html=result.html if need_html else None,
            status_code=result.status_code,
            fetched_at=fetched_at,
            error=None if result.success else (result.error_message or "未知错误"),
        )
    except Exception as exc:
        return FetchResult(
            url=url,
            markdown=None,
            html=None,
            status_code=None,
            fetched_at=fetched_at,
            error=str(exc),
        )


def _ensure_windows_event_loop() -> None:
    """Windows 11 上 Playwright 需要 ProactorEventLoop。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
