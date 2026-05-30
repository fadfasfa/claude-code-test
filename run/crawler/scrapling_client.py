"""Scrapling 同步抓取客户端。

供 Codex 调用，独立于 run/scraping/ 现有业务爬虫；输入 URL，输出 HTML
和可选 CSS selector 命中结果。依赖安装见 requirements-scrapling.txt。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


FetchMode = Literal["get", "browser", "stealthy"]


@dataclass
class FetchResult:
    url: str
    html: str | None
    extracted: list[str] | None
    status_code: int | None
    fetched_at: str
    error: str | None


def _require_scrapling() -> None:
    """Scrapling 未安装时给出可执行提示，避免裸 ImportError。"""
    try:
        import scrapling  # noqa: F401
    except ImportError:
        raise ImportError(
            "scrapling 未安装。请执行：\n"
            "  pip install -r run/crawler/requirements-scrapling.txt\n"
            "  scrapling install  # 仅在需要 browser/stealthy mode 时执行"
        ) from None


def _response_html(response: object) -> str | None:
    """从 Scrapling Response 中取 HTML；body 始终作为 bytes 兜底。"""
    html = getattr(response, "html", None)
    if isinstance(html, str):
        return html

    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        encoding = getattr(response, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="replace")
    if isinstance(body, str):
        return body
    return None


def _response_status(response: object) -> int | None:
    """兼容 Scrapling Response 的 status/status_code 命名。"""
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def _extract_css(response: object, css_selector: str | None) -> list[str] | None:
    """返回 css_selector 的 getall 结果；未传 selector 时保持 None。"""
    if not css_selector:
        return None

    matches = response.css(css_selector)  # type: ignore[attr-defined]
    values = matches.getall()
    return [str(value) for value in values]


def fetch_page(
    url: str,
    *,
    mode: FetchMode = "get",
    timeout_ms: int = 30_000,
    headless: bool = True,
    wait_for: str | None = None,
    css_selector: str | None = None,
    proxy: str | None = None,
    network_idle: bool = False,
) -> FetchResult:
    """同步抓取单个页面，返回 HTML 和可选 selector 命中结果。

    `mode="get"` 适合普通页面；`mode="browser"` 适合动态网页；
    `mode="stealthy"` 仅在用户明确授权且目标合规时由调用方显式选择。
    """
    if mode not in ("get", "browser", "stealthy"):
        raise ValueError(f"不支持的抓取模式：{mode!r}")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")

    _require_scrapling()

    from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher  # type: ignore

    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        if mode == "get":
            # Scrapling 0.4.8 的 Fetcher/DynamicFetcher/StealthyFetcher 都按毫秒接收 timeout。
            kwargs: dict[str, object] = {"timeout": timeout_ms}
            if proxy:
                kwargs["proxy"] = proxy
            response = Fetcher.get(url, **kwargs)
        else:
            fetcher = DynamicFetcher if mode == "browser" else StealthyFetcher
            # 与 get 模式保持同一单位，避免调用方在不同 mode 下看到隐式超时漂移。
            kwargs = {
                "timeout": timeout_ms,
                "headless": headless,
                "network_idle": network_idle,
            }
            if wait_for:
                kwargs["wait_selector"] = wait_for
            if proxy:
                kwargs["proxy"] = proxy
            response = fetcher.fetch(url, **kwargs)

        return FetchResult(
            url=url,
            html=_response_html(response),
            extracted=_extract_css(response, css_selector),
            status_code=_response_status(response),
            fetched_at=fetched_at,
            error=None,
        )
    except Exception as exc:
        return FetchResult(
            url=url,
            html=None,
            extracted=None,
            status_code=None,
            fetched_at=fetched_at,
            error=str(exc),
        )
