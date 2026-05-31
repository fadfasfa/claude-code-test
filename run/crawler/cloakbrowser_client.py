"""CloakBrowser 同步抓取客户端。

本模块只负责启动 CloakBrowser 并返回 HTML；它不 import Scrapling，也不承担
ApexLoL 业务解析，便于后续独立替换任一底层抓取路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata


@dataclass
class CloakFetchResult:
    url: str
    html: str | None
    status_code: int | None
    fetched_at: str
    cloakbrowser_version: str | None
    error: str | None


def _cloakbrowser_version() -> str | None:
    try:
        return metadata.version("cloakbrowser")
    except metadata.PackageNotFoundError:
        return None


def _require_cloakbrowser() -> None:
    """CloakBrowser 未安装时给出 smoke 可执行提示。"""
    try:
        import cloakbrowser  # noqa: F401
    except ImportError:
        raise ImportError(
            "cloakbrowser 未安装。请执行：\n"
            "  pip install -r run/crawler/requirements-cloakbrowser.txt"
        ) from None


def fetch_page(
    url: str,
    *,
    timeout_ms: int = 30_000,
    headless: bool = True,
    humanize: bool = False,
) -> CloakFetchResult:
    """用 CloakBrowser 打开页面并返回最终 HTML。"""
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")

    _require_cloakbrowser()

    from cloakbrowser import launch  # type: ignore

    fetched_at = datetime.now(timezone.utc).isoformat()
    version = _cloakbrowser_version()
    browser = None
    try:
        browser = launch(headless=headless, humanize=humanize)
        page = browser.new_page()
        response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        html = page.content()
        status_code = response.status if response is not None else None
        return CloakFetchResult(
            url=url,
            html=html,
            status_code=status_code,
            fetched_at=fetched_at,
            cloakbrowser_version=version,
            error=None,
        )
    except Exception as exc:
        return CloakFetchResult(
            url=url,
            html=None,
            status_code=None,
            fetched_at=fetched_at,
            cloakbrowser_version=version,
            error=str(exc),
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
