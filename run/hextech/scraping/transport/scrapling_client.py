"""Scrapling 同步抓取客户端。

供 Codex 调用，独立于 run/scraping/ 现有业务爬虫；输入 URL，输出 HTML
和可选 CSS selector 命中结果。依赖安装见 requirements-scrapling.txt。

调用方: scraping.hextech.scraper、scraping.synergy.mayhem_combo_scraper、scraping.synergy.scraper; 关键依赖: 见 imports。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal


FetchMode = Literal["get", "browser", "stealthy"]


@dataclass
class FetchResult:
    url: str
    html: str | None
    extracted: list[str] | None
    status_code: int | None
    fetched_at: str
    error: str | None
    error_kind: str = ""
    attempts: int = 1


@dataclass
class ScraplingFetchResult:
    """业务抓取端使用的纯文本结果，避免各 scraper 直接处理 Scrapling Response。"""

    url: str
    text: str
    status_code: int | None
    fetched_at: str
    error: str
    error_kind: str = ""
    attempts: int = 1

    def json(self) -> Any:
        return json.loads(self.text)


def _require_scrapling() -> None:
    """Scrapling 未安装时给出可执行提示，避免裸 ImportError。"""
    try:
        import scrapling  # noqa: F401
    except ImportError:
        raise ImportError(
            "scrapling 未安装。请执行：\n"
            r"  cd run" "\n"
            r"  .\.venv\Scripts\python.exe -m pip install -r requirements.txt" "\n"
            "  scrapling install  # 仅在需要 browser/stealthy mode 时执行"
        ) from None


def classify_fetch_error(error: object) -> str:
    """把 Scrapling/curl 异常归类为可诊断、可聚合的业务原因。"""

    text = str(error or "").lower()
    if not text:
        return ""
    if "curl: (35)" in text or "openssl_internal" in text or "tls connect" in text:
        return "tls_error"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text or "network" in text or "reset by peer" in text or "no active session" in text:
        return "network_error"
    return "scrapling_error"


def _is_retryable_fetch_error(error_kind: str) -> bool:
    return error_kind in {"tls_error", "timeout", "network_error"}


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


def _response_text(response: object) -> str:
    """兼容 body/content/html/text；JSON 原文优先从 body/content 取。"""
    for attr in ("body", "content"):
        value = getattr(response, attr, None)
        if isinstance(value, bytes):
            encoding = getattr(response, "encoding", None) or "utf-8"
            return value.decode(encoding, errors="replace")
        if isinstance(value, str):
            return value

    for attr in ("html", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str):
            return value
    return ""


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
    max_attempts: int = 2,
    retry_backoff_seconds: float = 0.35,
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

    if mode == "get":
        attempts = max(1, int(max_attempts))
        last_error = ""
        last_kind = ""
        fetched_at = ""
        for attempt in range(1, attempts + 1):
            fetched_at = datetime.now(timezone.utc).isoformat()
            try:
                # Scrapling 0.4.9 的页面 Fetcher 在 retries=0 时可能提前释放会话。
                # 这里保留一次内部 retry；业务高频链路使用 fetch_text 的结构化重试。
                kwargs: dict[str, object] = {"timeout": timeout_ms / 1000, "retries": 1}
                if proxy:
                    kwargs["proxy"] = proxy
                response = Fetcher.get(url, **kwargs)
                return FetchResult(
                    url=url,
                    html=_response_html(response),
                    extracted=_extract_css(response, css_selector),
                    status_code=_response_status(response),
                    fetched_at=fetched_at,
                    error=None,
                    error_kind="",
                    attempts=attempt,
                )
            except Exception as exc:
                last_error = str(exc)
                last_kind = classify_fetch_error(exc)
                if attempt < attempts and _is_retryable_fetch_error(last_kind):
                    time.sleep(max(0.0, float(retry_backoff_seconds)))
                    continue
                break
        return FetchResult(
            url=url,
            html=None,
            extracted=None,
            status_code=None,
            fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(),
            error=last_error,
            error_kind=last_kind or classify_fetch_error(last_error),
            attempts=attempt,
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
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
            error_kind="",
            attempts=1,
        )
    except Exception as exc:
        return FetchResult(
            url=url,
            html=None,
            extracted=None,
            status_code=None,
            fetched_at=fetched_at,
            error=str(exc),
            error_kind=classify_fetch_error(exc),
            attempts=1,
        )


def fetch_text(
    url: str,
    *,
    timeout_ms: int = 30_000,
    headers: dict[str, str] | None = None,
    impersonate: str = "chrome",
    stealthy_headers: bool = True,
    caller: str = "",
    max_attempts: int = 2,
    retry_backoff_seconds: float = 0.35,
) -> ScraplingFetchResult:
    """用 Scrapling Fetcher 普通 HTTP GET 获取原文。"""
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")
    if max_attempts <= 0:
        raise ValueError("max_attempts 必须大于 0")

    _require_scrapling()

    from scrapling.fetchers import Fetcher  # type: ignore

    attempts = max(1, int(max_attempts))
    last_error = ""
    last_kind = ""
    fetched_at = ""
    for attempt in range(1, attempts + 1):
        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            # Scrapling 0.4.9 在 retries=0 时可能提前释放会话；短文本链路也要
            # 保留一次内部 retry，否则会间歇性报 No active session available。
            response = Fetcher.get(
                url,
                timeout=timeout_ms / 1000,
                headers=headers,
                impersonate=impersonate,
                stealthy_headers=stealthy_headers,
                retries=1,
            )
            return ScraplingFetchResult(
                url=url,
                text=_response_text(response),
                status_code=_response_status(response),
                fetched_at=fetched_at,
                error="",
                error_kind="",
                attempts=attempt,
            )
        except Exception as exc:
            last_error = str(exc)
            last_kind = classify_fetch_error(exc)
            if attempt < attempts and _is_retryable_fetch_error(last_kind):
                time.sleep(max(0.0, float(retry_backoff_seconds)))
                continue
            break

    prefix = f"{caller}: " if caller else ""
    return ScraplingFetchResult(
        url=url,
        text="",
        status_code=None,
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(),
        error=f"{prefix}{last_error}",
        error_kind=last_kind or classify_fetch_error(last_error),
        attempts=attempt,
    )


def fetch_stealthy_text(
    url: str,
    *,
    timeout_ms: int = 35_000,
    wait_ms: int = 1_500,
    headless: bool = True,
    network_idle: bool = True,
    wait_selector: str | None = None,
    solve_cloudflare: bool = True,
    user_data_dir: str = "",
    real_chrome: bool = False,
    cdp_url: str = "",
) -> ScraplingFetchResult:
    """用独立运行态 profile 的 StealthyFetcher 获取 Apex 受保护页面。"""
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须大于 0")
    if wait_ms < 0:
        raise ValueError("wait_ms 不能小于 0")

    _require_scrapling()

    from scrapling.fetchers import StealthyFetcher  # type: ignore

    fetched_at = datetime.now(timezone.utc).isoformat()
    kwargs: dict[str, object] = {
        "timeout": timeout_ms,
        "wait": wait_ms,
        "headless": headless,
        "network_idle": network_idle,
        "solve_cloudflare": solve_cloudflare,
        "real_chrome": real_chrome,
    }
    if wait_selector:
        kwargs["wait_selector"] = wait_selector
    if user_data_dir:
        kwargs["user_data_dir"] = user_data_dir
    if cdp_url:
        kwargs["cdp_url"] = cdp_url

    try:
        response = StealthyFetcher.fetch(url, **kwargs)
        return ScraplingFetchResult(
            url=url,
            text=_response_text(response),
            status_code=_response_status(response),
            fetched_at=fetched_at,
            error="",
            error_kind="",
            attempts=1,
        )
    except Exception as exc:
        return ScraplingFetchResult(
            url=url,
            text="",
            status_code=None,
            fetched_at=fetched_at,
            error=str(exc),
            error_kind=classify_fetch_error(exc),
            attempts=1,
        )


def fetch_json(
    url: str,
    *,
    timeout_ms: int = 30_000,
    expected_kind: type | tuple[type, ...] | None = None,
    headers: dict[str, str] | None = None,
    caller: str = "",
) -> tuple[Any | None, ScraplingFetchResult]:
    """获取并解析 JSON；解析失败时把原因写入 result.error。"""
    result = fetch_text(url, timeout_ms=timeout_ms, headers=headers, caller=caller)
    if result.error:
        return None, result

    try:
        payload = result.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, replace(result, error=f"json_decode_error:{exc}", error_kind="json_decode_error")

    if expected_kind is not None and not isinstance(payload, expected_kind):
        return None, replace(result, error=f"json_kind_mismatch:{type(payload).__name__}", error_kind="json_kind_mismatch")

    return payload, result


__all__ = [
    "FetchResult",
    "FetchMode",
    "ScraplingFetchResult",
    "classify_fetch_error",
    "fetch_json",
    "fetch_page",
    "fetch_stealthy_text",
    "fetch_text",
]
