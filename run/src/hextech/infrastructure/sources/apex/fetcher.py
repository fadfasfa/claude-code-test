"""Apex 抓取职责拆分模块。"""
from __future__ import annotations

import hashlib
from typing import Callable

from hextech.infrastructure.persistence.raw_responses import RawResponseCache
from hextech.infrastructure.transport.conditional_response import (
    ConditionalFetchResult,
    ConditionalResponseCache,
    fetch_conditional,
)

from hextech.infrastructure.sources.apex.common import (
    APEX_ACCESS_DENIED_MARKER,
    APEX_NEXT_ERROR_MARKERS,
    APEX_ONLINE_FETCH_DELAY_SECONDS,
    APEX_ORIGIN_SYNERGY_MARKERS,
    BUNDLE_APP_JS_PATTERN,
    CHAMPION_DETAIL_HREF_PATTERN,
    DEFAULT_APEX_MANUAL_SNAPSHOT_DIR,
    DEFAULT_APEX_SNAPSHOT_DIR,
    FetchedResource,
    Iterable,
    MAX_JSON_RESOURCE_SIZE,
    Optional,
    Path,
    REQUEST_TIMEOUT_SECONDS,
    SCRIPT_SRC_PATTERN,
    ScraplingFetchResult,
    _safe_exception_label,
    _sanitize_url_for_log,
    env_flag,
    env_int,
    fetch_browser_page,
    fetch_text,
    get_request_user_agent,
    json,
    logger,
    os,
    re,
    time,
    urljoin,
    urlparse,
    urlunparse,
)
class ApexSource:
    """同源页面/资源获取层。"""

    def __init__(self, *, raw_cache: RawResponseCache | None = None,
                 conditional_cache: ConditionalResponseCache | None = None,
                 allow_browser: Callable[[], bool] | None = None):
        self.raw_cache = raw_cache
        self.conditional_cache = conditional_cache
        self.allow_browser = allow_browser or (lambda: True)
        self.base_url = os.environ.get("APEX_BASE_URL", "https://apexlol.info/zh").rstrip("/")
        parsed_base = urlparse(self.base_url)
        if parsed_base.scheme != "https" or not parsed_base.netloc:
            raise ValueError("APEX_BASE_URL 必须是有效的 https URL")
        self.allowed_netloc = parsed_base.netloc
        self.allowed_json_netlocs = self._build_allowed_json_netlocs()
        self.blocked = False
        self.last_fetch_error = ""
        logger.info("ApexSource 初始化完成：base=%s", _sanitize_url_for_log(self.base_url))

    def close(self) -> None:
        return None

    def is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc == self.allowed_netloc

    def is_allowed_json_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() in self.allowed_json_netlocs

    def _build_allowed_json_netlocs(self) -> set[str]:
        extra_hosts = {
            host.strip().lower()
            for host in os.getenv("APEX_JSON_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        }
        return {self.allowed_netloc.lower(), *extra_hosts}

    def build_allowed_url(self, href: str) -> Optional[str]:
        candidate = urljoin(f"{self.base_url}/", str(href or "").strip())
        if not self.is_allowed_url(candidate):
            logger.warning("跳过非白名单链接：%s", _sanitize_url_for_log(candidate))
            return None
        parsed = urlparse(candidate)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _resource_is_origin_success(self, resource: Optional[FetchedResource], *, is_detail: bool) -> bool:
        if resource is None or resource.error or not resource.text:
            return False
        if resource.status_code is not None and resource.status_code >= 400:
            return False
        if self._is_cloudflare_block(resource.text):
            self.blocked = True
            return False
        if is_detail and self._origin_failure_reason(resource.text):
            return False
        return True

    def _scrapling_result_to_resource(
        self,
        result: ScraplingFetchResult | ConditionalFetchResult,
        *,
        source: str,
    ) -> FetchedResource:
        error = result.error or None
        if result.status_code in {401, 403, 429}:
            self.blocked = True
            error = error or f"http_{result.status_code}"
        if result.text and self._is_cloudflare_block(result.text):
            self.blocked = True
            error = error or "cloudflare_block"
        if error:
            self.last_fetch_error = error
        return FetchedResource(
            url=result.url or self.base_url,
            text=result.text or "",
            source=getattr(result, "backend", "") or source,
            status_code=result.status_code or 0,
            error=error,
            not_modified=bool(getattr(result, "not_modified", False)),
            from_cache=bool(getattr(result, "from_cache", False)),
            body_sha256=hashlib.sha256((result.text or "").encode("utf-8")).hexdigest()
            if result.text else "",
            request_key=str(getattr(result, "request_key", "") or ""),
            response_headers=dict(getattr(result, "response_headers", {}) or {}),
        )

    def fetch_plain(self, url: str) -> Optional[FetchedResource]:
        """使用 Scrapling Fetcher 普通 HTTP 获取。"""

        if not self.is_allowed_url(url):
            logger.warning("拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None

        cached = self._cached(url)
        if cached is not None:
            return cached
        # Transport owns retry/backoff/circuit policy; never multiply it here.
        if self.conditional_cache is not None:
            result = fetch_conditional(
                fetch_text,
                url,
                cache=self.conditional_cache,
                headers={"User-Agent": get_request_user_agent()},
                fetch_kwargs={"timeout_ms": REQUEST_TIMEOUT_SECONDS * 1000},
            )
        else:
            result = fetch_text(
                url,
                timeout_ms=REQUEST_TIMEOUT_SECONDS * 1000,
                headers={"User-Agent": get_request_user_agent()},
            )
        resource = self._scrapling_result_to_resource(result, source="scrapling-get")
        if getattr(result, "error_kind", "") in {"circuit_open", "access_denied", "rate_limited"}:
            self.blocked = True
        self._save(url, resource)
        return resource

    def _cached(self, url: str) -> Optional[FetchedResource]:
        if self.raw_cache is not None and self.conditional_cache is None:
            # Backend is part of the local key so replay preserves provenance
            # without modifying the cached raw UTF-8 HTML body.
            for backend in ("http", "requests_fallback", "browser"):
                body = self.raw_cache.get(f"{url}#apex-backend={backend}")
                if body is not None:
                    return FetchedResource(url=url, text=body.decode("utf-8"),
                                           source=backend, status_code=200)
        return None

    def _save(self, url: str, resource: FetchedResource) -> None:
        if (self.raw_cache is not None and self.conditional_cache is None
                and resource.status_code == 200
                and self._resource_is_origin_success(resource, is_detail="/champions/" in urlparse(url).path)):
            self.raw_cache.put(f"{url}#apex-backend={resource.source}", resource.text.encode("utf-8"))

    def fetch(
        self,
        url: str,
        *,
        allow_browser: bool = False,
    ) -> Optional[FetchedResource]:
        is_detail = "/champions/" in urlparse(url).path
        plain_resource = self.fetch_plain(url)
        if self._resource_is_origin_success(plain_resource, is_detail=is_detail):
            return plain_resource
        if (not allow_browser or not self.allow_browser() or self.blocked
                or plain_resource is None or not plain_resource.text
                or plain_resource.status_code in {401, 403, 429}
                or "circuit" in str(plain_resource.error or "").lower()):
            return plain_resource

        rendered = fetch_browser_page(
            url,
            mode="browser",
            timeout_ms=max(1, env_int("APEX_BROWSER_TIMEOUT_SECONDS", 25)) * 1000,
            network_idle=True,
        )
        browser_resource = FetchedResource(
            url=url,
            text=rendered.html or "",
            source=getattr(rendered, "backend", "browser") or "browser",
            status_code=rendered.status_code or 0,
            error=rendered.error,
            body_sha256=hashlib.sha256((rendered.html or "").encode("utf-8")).hexdigest()
            if rendered.html else "",
            response_headers=dict(getattr(rendered, "response_headers", {}) or {}),
        )
        if self._resource_is_origin_success(browser_resource, is_detail=is_detail):
            self._save(url, browser_resource)
            return browser_resource
        return plain_resource

    def fetch_configured_json_resource(self) -> Optional[FetchedResource]:
        raw_url = os.getenv("APEX_SYNERGY_JSON_URL", "").strip()
        if not raw_url:
            return None
        if not self.is_allowed_json_url(raw_url):
            logger.error("APEX_SYNERGY_JSON_URL 不在允许的 https host 内：%s", _sanitize_url_for_log(raw_url))
            return None
        result = fetch_text(
            raw_url,
            timeout_ms=REQUEST_TIMEOUT_SECONDS * 1000,
            headers={"User-Agent": get_request_user_agent()},
        )
        resource = self._scrapling_result_to_resource(result, source="json-url")
        if resource.error or not resource.text:
            logger.warning(
                "APEX_SYNERGY_JSON_URL 读取失败：url=%s status=%s error=%s",
                _sanitize_url_for_log(raw_url),
                resource.status_code,
                resource.error or "empty_response",
            )
            return None
        if len(resource.text.encode("utf-8")) > MAX_JSON_RESOURCE_SIZE:
            logger.error("APEX_SYNERGY_JSON_URL 响应过大，已拒绝：%s", _sanitize_url_for_log(raw_url))
            return None
        try:
            payload = json.loads(resource.text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("APEX_SYNERGY_JSON_URL JSON 解析失败：url=%s error=%s", _sanitize_url_for_log(raw_url), _safe_exception_label(exc))
            return None
        if not isinstance(payload, (dict, list)):
            logger.error("APEX_SYNERGY_JSON_URL 不是 JSON object/list：%s", _sanitize_url_for_log(raw_url))
            return None
        resource.text = json.dumps(payload, ensure_ascii=False)
        return resource

    def discover_resources(self) -> list[FetchedResource]:
        snapshot_resources = self._load_snapshot_resources()
        if snapshot_resources:
            logger.info("使用 Apex 本地 snapshot 资源：count=%s", len(snapshot_resources))
            return snapshot_resources

        if not env_flag("APEX_ALLOW_ONLINE_FETCH", "0"):
            logger.error("未找到 Apex snapshot，且 APEX_ALLOW_ONLINE_FETCH 未启用；保留旧协同快照")
            return []

        json_resource = self.fetch_configured_json_resource()
        seeds = [self.base_url, f"{self.base_url}/champions", f"{self.base_url}/hextech"]
        resources: list[FetchedResource] = []
        seen_urls = set()
        script_urls = []
        online_delay = float(os.getenv("APEX_ONLINE_FETCH_DELAY_SECONDS", str(APEX_ONLINE_FETCH_DELAY_SECONDS)) or "0")

        for url in seeds:
            resource = self.fetch(url)
            if not resource or resource.url in seen_urls:
                continue
            seen_urls.add(resource.url)
            resources.append(resource)
            script_urls.extend(self._extract_script_urls(resource.text))
            if online_delay > 0:
                time.sleep(online_delay)

        detail_urls = self._extract_champion_detail_urls(resources)
        for detail_url in detail_urls:
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            resource = self.fetch(detail_url, allow_browser=True)
            if resource:
                resources.append(resource)
            if online_delay > 0:
                time.sleep(online_delay)

        if not detail_urls or os.getenv("APEX_FETCH_JS_CHUNKS", "0").strip() == "1":
            for script_url in script_urls:
                if script_url in seen_urls:
                    continue
                seen_urls.add(script_url)
                script = self.fetch(script_url)
                if script:
                    resources.append(script)
                if online_delay > 0:
                    time.sleep(online_delay)

        if json_resource and json_resource.url not in seen_urls:
            resources.append(json_resource)

        return resources

    def _extract_champion_detail_urls(self, resources: Iterable[FetchedResource]) -> list[str]:
        max_pages = int(os.getenv("APEX_MAX_CHAMPION_DETAIL_PAGES", "0") or "0")
        urls = []
        for resource in resources:
            for raw_href in CHAMPION_DETAIL_HREF_PATTERN.findall(resource.text or ""):
                candidate = self.build_allowed_url(raw_href)
                if candidate and candidate not in urls:
                    urls.append(candidate)
                    if max_pages > 0 and len(urls) >= max_pages:
                        return urls
        return urls

    def _load_snapshot_resources(self) -> list[FetchedResource]:
        raw_snapshot_dir = os.getenv("APEX_SNAPSHOT_DIR", "").strip()
        snapshot_dir = (
            Path(raw_snapshot_dir).expanduser().resolve()
            if raw_snapshot_dir
            else Path(DEFAULT_APEX_MANUAL_SNAPSHOT_DIR).resolve()
        )

        allowed_root = Path(DEFAULT_APEX_SNAPSHOT_DIR).resolve()
        try:
            snapshot_dir.relative_to(allowed_root)
        except ValueError:
            logger.error("APEX_SNAPSHOT_DIR 必须位于 %s 下：%s", allowed_root, snapshot_dir)
            return []
        if not snapshot_dir.exists() or not snapshot_dir.is_dir():
            logger.warning("Apex snapshot 目录不存在或不是目录：%s", snapshot_dir)
            return []

        resources: list[FetchedResource] = []
        allowed_suffixes = {".html", ".htm", ".js", ".json", ".txt"}
        for path in sorted(snapshot_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            try:
                path.relative_to(snapshot_dir)
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                logger.warning("跳过 Apex snapshot 文件：file=%s error=%s", path.name, _safe_exception_label(exc))
                continue
            if not text.strip():
                continue
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.relative_to(snapshot_dir).as_posix())
            resources.append(FetchedResource(
                url=f"{self.base_url}/snapshot/{safe_name}",
                text=text,
                source="snapshot",
                status_code=200,
            ))
        return resources

    def _extract_script_urls(self, html: str) -> list[str]:
        urls = []
        for raw_src in SCRIPT_SRC_PATTERN.findall(html or ""):
            candidate = self.build_allowed_url(raw_src)
            if candidate:
                urls.append(candidate)
        for match in BUNDLE_APP_JS_PATTERN.findall(html or ""):
            candidate = self.build_allowed_url(match)
            if candidate:
                urls.append(candidate)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _is_cloudflare_block(text: str) -> bool:
        lowered = (text or "")[:8000].lower()
        legacy_block = "attention required" in lowered and "cloudflare" in lowered
        strong_challenge = "challenges.cloudflare.com" in lowered or "_cf_chl_opt" in lowered
        managed_challenge = "just a moment" in lowered or "请稍候" in lowered
        if strong_challenge:
            return True
        return legacy_block or managed_challenge

    @classmethod
    def _origin_failure_reason(cls, text: str) -> str:
        """识别 CloakBrowser 终态是否真是 ApexLoL 英雄联动 origin 页面。"""
        html = text or ""
        lowered = html[:200_000].lower()
        stripped_text = re.sub(r"\s+", " ", html).strip().lower()
        if not html:
            return "empty_html"
        if cls._is_cloudflare_block(html):
            return "cloudflare_block"
        if len(html.encode("utf-8", errors="ignore")) <= 2048 and APEX_ACCESS_DENIED_MARKER in stripped_text:
            return "access_denied"
        if any(marker in lowered for marker in APEX_NEXT_ERROR_MARKERS):
            return "origin_5xx_error_page"
        if not any(marker in html for marker in APEX_ORIGIN_SYNERGY_MARKERS):
            return "missing_origin_synergy_hydration"
        return ""
