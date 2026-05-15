"""ApexLoL 海克斯联动数据抓取器。

抓取端分三层：
- ``ApexSource`` 负责同源页面和资源获取，普通 requests 失败后可切到 Selenium。
- ``SynergyExtractor`` 负责从页面 hydration 数据、bundle 或旧 marker 中提取联动对象。
- ``SynergyWriter`` 负责把结构化对象写回现有 ``Champion_Synergy.json`` 兼容格式。
"""

from __future__ import annotations

import ast
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from processing.runtime_store import build_synergy_data_path, get_latest_csv
from scraping.icon_resolver import normalize_augment_name
from scraping.version_sync import STATIC_DATA_DIR
from tools.log_utils import install_summary_logging, log_task_summary


def _get_script_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap_runtime_base_dir() -> str:
    runtime_base = os.getenv("HEXTECH_BASE_DIR", "").strip()
    if runtime_base:
        return os.path.abspath(runtime_base)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _get_script_dir()


BASE_DIR = _bootstrap_runtime_base_dir()
SELENIUM_CACHE_DIR = os.path.join(BASE_DIR, "data", "runtime", "cache", "selenium")
SELENIUM_PROFILE_DIR = os.path.join(BASE_DIR, "data", "runtime", "profile", "apex_selenium")
DEFAULT_APEX_SNAPSHOT_DIR = os.path.join(BASE_DIR, "data", "runtime", "cache", "apex_snapshot")
STATIC_DATA_PATH = Path(STATIC_DATA_DIR)
ALLOWED_STATIC_DATA_FILES = {"Champion_Core_Data.json"}
MAX_STATIC_DATA_FILE_SIZE = 10 * 1024 * 1024
MAX_FETCH_RETRIES = 1
REQUEST_TIMEOUT_SECONDS = 6
RETRY_BACKOFF_FACTOR = 0.5
OUTPUT_LOCK_TIMEOUT_SECONDS = 5
OUTPUT_LOCK_POLL_INTERVAL_SECONDS = 0.2
BUNDLE_INTERACTION_SECTION_MARKER = "fx={manual:gx},"
BUNDLE_APP_JS_PATTERN = re.compile(r'/assets/app\.[^"\']+\.js')
SCRIPT_SRC_PATTERN = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
JSON_SCRIPT_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
HYDRATION_PATTERN = re.compile(
    r'<script[^>]+id=["\'](?:__NEXT_DATA__|__NUXT_DATA__|__remixContext)["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
VISIBLE_RATING_PATTERN = re.compile(r"^(SSS|SS|S|A|B|C|D)\s*(?:Tier|级|评分)?(?:\s+|$)", re.IGNORECASE)
VISIBLE_STOP_LINE_PATTERN = re.compile(
    r"^(comments?|recommended|deprecated|edit|delete|reply|show more|login|sign in|"
    r"评论|推荐|已弃用|编辑|删除|回复|加载更多|登录|登入)$",
    re.IGNORECASE,
)
SYNERGY_TAG_LABELS = {
    "Synergy": "强力联动",
    "Trap": "陷阱",
    "Fun": "娱乐",
    "Bug": "缺陷",
    "强力联动": "强力联动",
    "陷阱": "陷阱",
    "娱乐": "娱乐",
    "缺陷": "缺陷",
}
TIER_LABELS = {
    "Prismatic": "棱彩",
    "Gold": "黄金",
    "Silver": "白银",
    "棱彩": "棱彩",
    "彩色": "棱彩",
    "黄金": "黄金",
    "金色": "黄金",
    "白银": "白银",
    "银色": "白银",
}

install_summary_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]


@dataclass
class FetchedResource:
    url: str
    text: str
    source: str
    status_code: int = 200


@dataclass
class ChampionInfo:
    id: str
    name: str
    title: str
    en_name: str
    aliases: list[str] = field(default_factory=list)
    slug: str = ""


@dataclass
class SynergyEntry:
    champion_slug: str
    augment_names: list[str]
    tier: str
    rating: str
    tag: str
    author: str
    is_original: bool
    content: str
    upvotes: int = 0
    downvotes: int = 0

    def to_compat_string(self) -> str:
        augment_text = ", ".join(dict.fromkeys(self.augment_names))
        originality = "原创" if self.is_original else "非原创"
        return " | ".join(
            [
                augment_text,
                self.tier or "黄金",
                f"评分 {self.rating or '未知'}",
                self.tag or "强力联动",
                str(max(0, int(self.upvotes or 0))),
                str(max(0, int(self.downvotes or 0))),
                f"作者：{self.author or 'ApexLoL'}",
                originality,
                self.content,
            ]
        )


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def normalize_name(name_str: str) -> str:
    if not name_str:
        return ""
    return "".join(ch for ch in str(name_str).lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def normalize_slug(value: str) -> str:
    return normalize_augment_name(str(value or "").replace(" ", "").replace("_", "").replace("-", ""))


def _sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:200]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _safe_exception_label(exc: Exception) -> str:
    return exc.__class__.__name__


def _clean_text(value: Any) -> str:
    text = unescape(str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_tier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "黄金"
    if text in TIER_LABELS:
        return TIER_LABELS[text]
    lowered = text.lower()
    if "prismatic" in lowered or "棱彩" in text or "彩色" in text:
        return "棱彩"
    if "gold" in lowered or "黄金" in text or "金色" in text:
        return "黄金"
    if "silver" in lowered or "白银" in text or "银色" in text:
        return "白银"
    return text


def normalize_tag(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            tag = normalize_tag(item)
            if tag:
                return tag
        return "强力联动"
    text = str(value or "").strip()
    if not text:
        return "强力联动"
    return SYNERGY_TAG_LABELS.get(text, text)


def _resolve_static_data_path(filename: str) -> Path:
    base_name = os.path.basename(filename)
    if base_name != filename or filename not in ALLOWED_STATIC_DATA_FILES:
        raise ValueError(f"不允许访问的配置文件：{filename}")

    resolved = (STATIC_DATA_PATH / filename).resolve()
    static_root = STATIC_DATA_PATH.resolve()
    if resolved.parent != static_root:
        raise ValueError(f"配置文件路径越界：{filename}")
    return resolved


def _load_json_file(filename: str, expected_kind: str) -> dict:
    file_path = _resolve_static_data_path(filename)
    if not file_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{filename}")
    if file_path.stat().st_size > MAX_STATIC_DATA_FILE_SIZE:
        raise ValueError(f"配置文件过大：{filename}")

    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{expected_kind} 配置格式错误：{filename}")
    if expected_kind == "core_data":
        for champ_id, champ_info in data.items():
            if not isinstance(champ_id, str) or not isinstance(champ_info, dict):
                raise ValueError(f"{expected_kind} 配置内容格式错误：{filename}")
    return data


@contextmanager
def _output_file_lock(lock_path: Path, timeout_seconds: int = OUTPUT_LOCK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    lock_fd = None
    try:
        while True:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                try:
                    stale_age = time.time() - lock_path.stat().st_mtime
                    if stale_age > timeout_seconds * 4:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待输出锁超时：{lock_path.name}")
                time.sleep(OUTPUT_LOCK_POLL_INTERVAL_SECONDS)
        yield
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(output_path.parent),
            delete=False,
            suffix=".tmp",
        ) as f:
            temp_path = Path(f.name)
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def build_core_info(core_data: dict) -> dict[str, ChampionInfo]:
    result = {}
    for champ_id, champ_info in core_data.items():
        name = str(champ_info.get("name") or "").strip()
        if not name:
            continue
        en_name = str(champ_info.get("en_name") or "").strip()
        title = str(champ_info.get("title") or "").strip()
        aliases = champ_info.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        result[str(champ_id)] = ChampionInfo(
            id=str(champ_id),
            name=name,
            title=title,
            en_name=en_name,
            aliases=[str(item).strip() for item in aliases if str(item).strip()],
            slug=normalize_slug(en_name or title or name),
        )
    return result


def build_champion_lookup(core_info: dict[str, ChampionInfo]) -> dict[str, ChampionInfo]:
    lookup = {}
    for champ in core_info.values():
        values = [champ.id, champ.name, champ.title, champ.en_name, champ.slug, *champ.aliases]
        for value in values:
            normalized = normalize_name(value)
            if normalized:
                lookup.setdefault(normalized, champ)
            slug = normalize_slug(value)
            if slug:
                lookup.setdefault(slug, champ)
    return lookup


class ApexSource:
    """同源页面/资源获取层。"""

    def __init__(self):
        self.base_url = os.environ.get("APEX_BASE_URL", "https://apexlol.info/zh").rstrip("/")
        parsed_base = urlparse(self.base_url)
        if parsed_base.scheme != "https" or not parsed_base.netloc:
            raise ValueError("APEX_BASE_URL 必须是有效的 https URL")
        self.allowed_netloc = parsed_base.netloc
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": get_random_user_agent()})
        self._browser_driver = None
        self._browser_timeout = int(os.getenv("APEX_BROWSER_TIMEOUT_SECONDS", "18") or "18")
        self._profile_root = os.path.join(SELENIUM_PROFILE_DIR, f"session-{os.getpid()}-{int(time.time())}")
        os.makedirs(SELENIUM_CACHE_DIR, exist_ok=True)
        os.makedirs(self._profile_root, exist_ok=True)
        os.environ.setdefault("SE_CACHE_PATH", SELENIUM_CACHE_DIR)
        logger.info("ApexSource 初始化完成：base=%s", _sanitize_url_for_log(self.base_url))

    def close(self) -> None:
        if self._browser_driver is not None:
            try:
                self._browser_driver.quit()
            except Exception:
                pass
            self._browser_driver = None

    def is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc == self.allowed_netloc

    def build_allowed_url(self, href: str) -> Optional[str]:
        candidate = urljoin(f"{self.base_url}/", str(href or "").strip())
        if not self.is_allowed_url(candidate):
            logger.warning("跳过非白名单链接：%s", _sanitize_url_for_log(candidate))
            return None
        parsed = urlparse(candidate)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def fetch_requests(self, url: str) -> Optional[FetchedResource]:
        if not self.is_allowed_url(url):
            logger.warning("拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None

        retryable_status_codes = {429, 500, 502, 503, 504}
        for attempt in range(MAX_FETCH_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                response.encoding = "utf-8"
                if response.status_code == 200 and not self._is_cloudflare_block(response.text):
                    return FetchedResource(url=url, text=response.text, source="requests", status_code=200)
                if response.status_code == 403 or self._is_cloudflare_block(response.text):
                    logger.warning("Apex 普通请求被拒绝：url=%s status=%s", _sanitize_url_for_log(url), response.status_code)
                    return None
                if response.status_code in retryable_status_codes and attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_FACTOR * (2 ** attempt))
                    continue
                logger.error("页面状态异常：url=%s status=%s", _sanitize_url_for_log(url), response.status_code)
                return None
            except requests.RequestException as exc:
                if attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_FACTOR * (2 ** attempt))
                    continue
                logger.error("页面加载失败：url=%s error=%s", _sanitize_url_for_log(url), _safe_exception_label(exc))
                return None
        return None

    def fetch(self, url: str, *, allow_browser: bool = True) -> Optional[FetchedResource]:
        resource = self.fetch_requests(url)
        if resource is not None:
            return resource
        if not allow_browser:
            return None
        return self.fetch_browser(url)

    def fetch_browser(self, url: str) -> Optional[FetchedResource]:
        if not self.is_allowed_url(url):
            logger.warning("浏览器拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None
        try:
            driver = self._get_browser_driver()
            driver.get(url)
            self._wait_for_browser_ready(driver)
            html = driver.page_source or ""
            if not html or self._is_cloudflare_block(html):
                logger.error("浏览器未取得可解析 Apex 页面：url=%s", _sanitize_url_for_log(url))
                return None
            return FetchedResource(url=url, text=html, source="selenium", status_code=200)
        except Exception as exc:
            logger.error(
                "浏览器访问失败：url=%s error=%s detail=%s",
                _sanitize_url_for_log(url),
                _safe_exception_label(exc),
                str(exc)[:240],
            )
            return None

    def discover_resources(self) -> list[FetchedResource]:
        snapshot_resources = self._load_snapshot_resources()
        if snapshot_resources:
            logger.info("使用 Apex 本地 snapshot 资源：count=%s", len(snapshot_resources))
            return snapshot_resources

        seeds = [self.base_url, f"{self.base_url}/champions", f"{self.base_url}/hextech"]
        resources: list[FetchedResource] = []
        seen_urls = set()
        script_urls = []

        for url in seeds:
            resource = self.fetch(url, allow_browser=True)
            if not resource or resource.url in seen_urls:
                continue
            seen_urls.add(resource.url)
            resources.append(resource)
            script_urls.extend(self._extract_script_urls(resource.text))

        for script_url in script_urls:
            if script_url in seen_urls:
                continue
            seen_urls.add(script_url)
            script = self.fetch(script_url, allow_browser=False)
            if script:
                resources.append(script)

        return resources

    def _load_snapshot_resources(self) -> list[FetchedResource]:
        raw_snapshot_dir = os.getenv("APEX_SNAPSHOT_DIR", "").strip()
        if not raw_snapshot_dir:
            return []

        snapshot_dir = Path(raw_snapshot_dir).expanduser().resolve()
        allowed_root = Path(DEFAULT_APEX_SNAPSHOT_DIR).resolve()
        try:
            snapshot_dir.relative_to(allowed_root)
        except ValueError:
            logger.error("APEX_SNAPSHOT_DIR 必须位于 %s 下：%s", allowed_root, snapshot_dir)
            return []
        if not snapshot_dir.exists() or not snapshot_dir.is_dir():
            logger.error("APEX_SNAPSHOT_DIR 不存在或不是目录：%s", snapshot_dir)
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
        return "attention required" in lowered and "cloudflare" in lowered

    def _get_browser_driver(self):
        if self._browser_driver is not None:
            return self._browser_driver

        browser = os.getenv("APEX_BROWSER", "auto").strip().lower() or "auto"
        headless = os.getenv("APEX_HEADLESS", "1").strip() != "0"
        errors = []

        if browser in {"auto", "edge"}:
            try:
                from selenium import webdriver
                from selenium.webdriver.edge.options import Options as EdgeOptions

                options = EdgeOptions()
                edge_binary = self._find_browser_binary("edge")
                if edge_binary:
                    options.binary_location = edge_binary
                if headless:
                    options.add_argument("--headless=new")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-crashpad")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--remote-debugging-port=0")
                options.add_argument("--window-size=1365,900")
                options.add_argument(f"--user-data-dir={os.path.join(self._profile_root, 'edge')}")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                driver = webdriver.Edge(options=options)
                driver.set_page_load_timeout(self._browser_timeout)
                self._browser_driver = driver
                logger.info("Apex Selenium 使用 Edge 启动")
                return driver
            except Exception as exc:
                errors.append(f"edge={_safe_exception_label(exc)}:{str(exc)[:160]}")

        if browser in {"auto", "chrome"}:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options as ChromeOptions

                options = ChromeOptions()
                chrome_binary = self._find_browser_binary("chrome")
                if chrome_binary:
                    options.binary_location = chrome_binary
                if headless:
                    options.add_argument("--headless=new")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-crashpad")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--remote-debugging-port=0")
                options.add_argument("--window-size=1365,900")
                options.add_argument(f"--user-data-dir={os.path.join(self._profile_root, 'chrome')}")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                driver = webdriver.Chrome(options=options)
                driver.set_page_load_timeout(self._browser_timeout)
                self._browser_driver = driver
                logger.info("Apex Selenium 使用 Chrome 启动")
                return driver
            except Exception as exc:
                errors.append(f"chrome={_safe_exception_label(exc)}:{str(exc)[:160]}")

        raise RuntimeError("无法启动 Selenium 浏览器：" + ", ".join(errors))

    @staticmethod
    def _find_browser_binary(kind: str) -> str:
        if kind == "edge":
            env_value = os.getenv("APEX_EDGE_BINARY", "").strip()
            candidates = [
                env_value,
                os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
        else:
            env_value = os.getenv("APEX_CHROME_BINARY", "").strip()
            candidates = [
                env_value,
                os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ]
        return next((path for path in candidates if path and os.path.exists(path)), "")

    def _wait_for_browser_ready(self, driver) -> None:
        deadline = time.monotonic() + self._browser_timeout
        while time.monotonic() < deadline:
            try:
                state = driver.execute_script("return document.readyState")
                if state == "complete":
                    time.sleep(1.0)
                    return
            except Exception:
                return
            time.sleep(0.25)


class SynergyExtractor:
    """从 HTML/JS/JSON 资源中提取结构化联动对象。"""

    def __init__(self, champion_lookup: dict[str, ChampionInfo], augment_name_map: dict[str, str]):
        self.champion_lookup = champion_lookup
        self.augment_name_map = augment_name_map

    def extract(self, resources: Iterable[FetchedResource]) -> dict[str, list[SynergyEntry]]:
        results: dict[str, list[SynergyEntry]] = {}
        errors = []
        for resource in resources:
            try:
                for entry in self._extract_from_resource(resource):
                    results.setdefault(entry.champion_slug, []).append(entry)
            except Exception as exc:
                errors.append(f"{Path(urlparse(resource.url).path).name or resource.url}:{_safe_exception_label(exc)}")
                logger.debug("资源解析失败：%s", _sanitize_url_for_log(resource.url), exc_info=True)

        if results:
            return self._dedupe_entries(results)

        raise ValueError("联动解析结果为空" + (f"；errors={';'.join(errors[:6])}" if errors else ""))

    def _extract_from_resource(self, resource: FetchedResource) -> list[SynergyEntry]:
        text = resource.text or ""
        entries = []
        if "<html" in text[:1000].lower():
            entries.extend(self._extract_from_html(text, resource.url))
        if text.strip().startswith(("{", "[")):
            try:
                entries.extend(self._extract_from_json_payload(json.loads(text), fallback_slug=""))
            except json.JSONDecodeError:
                pass
        entries.extend(self._extract_old_bundle(text))
        entries.extend(self._extract_generic_js_objects(text))
        return entries

    def _extract_from_html(self, html: str, url: str) -> list[SynergyEntry]:
        entries = []
        for match in HYDRATION_PATTERN.findall(html) + JSON_SCRIPT_PATTERN.findall(html):
            try:
                payload = json.loads(unescape(match).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            entries.extend(self._extract_from_json_payload(payload, fallback_slug=self._slug_from_url(url)))
        entries.extend(self._extract_from_visible_html_text(html, url))
        return entries

    def _extract_from_visible_html_text(self, html: str, url: str) -> list[SynergyEntry]:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        lines = []
        for raw_line in soup.get_text("\n").splitlines():
            line = _clean_text(raw_line)
            if line and line not in lines[-3:]:
                lines.append(line)
        if not lines:
            return []

        url_slug = self._slug_from_url(url)
        fallback_slug = url_slug if url_slug in self.champion_lookup else (self._slug_from_visible_lines(lines) or url_slug)
        entries = []
        for index, line in enumerate(lines):
            rating_match = VISIBLE_RATING_PATTERN.match(line)
            if not rating_match:
                continue

            augment_names = []
            tier = ""
            cursor = index - 1
            while cursor >= 0 and len(augment_names) < 4:
                current = lines[cursor]
                normalized_tier = normalize_tier(current)
                if normalized_tier != current or current in TIER_LABELS:
                    tier = tier or normalized_tier
                    cursor -= 1
                    continue
                resolved_names = self._resolve_known_augment_names(current)
                if resolved_names and self._looks_like_augment_name(current, resolved_names):
                    augment_names = resolved_names + augment_names
                    cursor -= 1
                    continue
                break

            if not augment_names:
                continue

            rating, tag = self._parse_visible_rating_tag(line)
            author, content = self._parse_visible_author_content(lines, index + 1)
            if not content:
                continue
            entries.append(SynergyEntry(
                champion_slug=fallback_slug,
                augment_names=list(dict.fromkeys(augment_names)),
                tier=tier or "黄金",
                rating=rating,
                tag=tag,
                author=author,
                is_original="原创" in line.lower() or "original" in line.lower(),
                content=content,
                upvotes=0,
                downvotes=0,
            ))
        return [entry for entry in entries if entry.champion_slug]

    def _parse_visible_rating_tag(self, line: str) -> tuple[str, str]:
        rating_match = VISIBLE_RATING_PATTERN.match(line or "")
        rating = rating_match.group(1).upper() if rating_match else "未知"
        lowered = (line or "").lower()
        if "trap" in lowered or "陷阱" in line:
            tag = "陷阱"
        elif "fun" in lowered or "娱乐" in line:
            tag = "娱乐"
        elif "bug" in lowered or "缺陷" in line:
            tag = "缺陷"
        else:
            tag = "强力联动"
        return rating, tag

    def _parse_visible_author_content(self, lines: list[str], start_index: int) -> tuple[str, str]:
        author = "ApexLoL"
        content_lines = []
        cursor = start_index
        while cursor < len(lines) and cursor < start_index + 3:
            candidate = lines[cursor]
            if candidate.startswith(("作者：", "作者:")):
                author = candidate.split(":", 1)[-1].split("：", 1)[-1].strip() or author
                cursor += 1
                break
            if (
                len(candidate) <= 40
                and not self._looks_like_augment_name(candidate, self._resolve_known_augment_names(candidate))
                and not VISIBLE_STOP_LINE_PATTERN.match(candidate)
            ):
                author = candidate
                cursor += 1
                break
            cursor += 1

        while cursor < len(lines) and len(content_lines) < 12:
            candidate = lines[cursor]
            if VISIBLE_RATING_PATTERN.match(candidate) or VISIBLE_STOP_LINE_PATTERN.match(candidate):
                break
            if self._looks_like_augment_name(candidate, self._resolve_known_augment_names(candidate)):
                next_line = lines[cursor + 1] if cursor + 1 < len(lines) else ""
                if normalize_tier(next_line) != next_line or next_line in TIER_LABELS:
                    break
            if not candidate.startswith(("作者：", "作者:")):
                content_lines.append(candidate)
            cursor += 1

        return author, _clean_text(" ".join(content_lines))

    def _looks_like_augment_name(self, raw_name: str, resolved_names: list[str]) -> bool:
        text = str(raw_name or "").strip()
        if not text or not resolved_names:
            return False
        normalized = normalize_augment_name(text)
        resolved_tokens = {normalize_augment_name(name) for name in resolved_names}
        if normalized in resolved_tokens:
            return True
        return normalized in self.augment_name_map or text in self.augment_name_map

    def _resolve_known_augment_names(self, raw_name: str) -> list[str]:
        key = str(raw_name or "").strip()
        if not key:
            return []
        candidates = [key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)]
        for candidate in candidates:
            resolved = self.augment_name_map.get(candidate)
            if resolved:
                return [resolved]
        return []

    def _slug_from_visible_lines(self, lines: list[str]) -> str:
        head_text = " ".join(lines[:80])
        normalized_head = normalize_name(head_text)
        for key, champion in sorted(self.champion_lookup.items(), key=lambda item: len(item[0]), reverse=True):
            if not key or key.isdigit() or len(key) <= 1:
                continue
            if key.isascii() and len(key) < 3:
                continue
            if key in normalized_head:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
        return ""

    def _extract_from_json_payload(self, payload: Any, fallback_slug: str) -> list[SynergyEntry]:
        entries = []
        for item, path in self._walk_json(payload):
            if not isinstance(item, dict):
                continue
            entry = self._entry_from_dict(item, fallback_slug=fallback_slug or self._slug_from_path(path))
            if entry:
                entries.append(entry)
        return entries

    def _walk_json(self, value: Any, path: tuple[str, ...] = ()):
        yield value, path
        if isinstance(value, dict):
            for key, child in value.items():
                yield from self._walk_json(child, (*path, str(key)))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                yield from self._walk_json(child, (*path, str(idx)))

    def _entry_from_dict(self, item: dict, fallback_slug: str) -> Optional[SynergyEntry]:
        augment_names = self._resolve_augment_names(item)
        if not augment_names:
            return None
        content = self._resolve_content(item)
        if not content:
            return None
        champion_slug = self._resolve_champion_slug(item, fallback_slug=fallback_slug)
        if not champion_slug:
            return None
        return SynergyEntry(
            champion_slug=champion_slug,
            augment_names=augment_names,
            tier=normalize_tier(item.get("tier") or item.get("rarity") or item.get("rank")),
            rating=str(item.get("rating") or item.get("grade") or item.get("score") or "").strip() or "未知",
            tag=normalize_tag(item.get("tags") or item.get("tag") or item.get("type")),
            author=str(item.get("author") or item.get("contributor") or item.get("user") or "ApexLoL").strip() or "ApexLoL",
            is_original=bool(item.get("isOriginal") or item.get("original")),
            content=content,
            upvotes=self._int_value(item.get("upvotes") or item.get("upVotes") or item.get("likes")),
            downvotes=self._int_value(item.get("downvotes") or item.get("downVotes") or item.get("dislikes")),
        )

    def _resolve_champion_slug(self, item: dict, fallback_slug: str) -> str:
        raw_values = [
            item.get("championSlug"),
            item.get("champion"),
            item.get("championName"),
            item.get("championId"),
            item.get("hero"),
            item.get("heroName"),
            fallback_slug,
        ]
        for raw in raw_values:
            if raw is None:
                continue
            normalized = normalize_name(raw)
            slug = normalize_slug(raw)
            champion = self.champion_lookup.get(normalized) or self.champion_lookup.get(slug)
            if champion:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
            if slug:
                return slug
        return ""

    def _resolve_content(self, item: dict) -> str:
        note = item.get("note") or item.get("content") or item.get("description") or item.get("text")
        if isinstance(note, dict):
            note = note.get("zh") or note.get("zh_CN") or note.get("cn") or note.get("en") or next(iter(note.values()), "")
        return _clean_text(note)

    def _resolve_augment_names(self, item: dict) -> list[str]:
        raw_values = []
        for key in ("hextechId", "hextechIds", "augmentId", "augmentIds", "augment", "augments", "augmentName", "augmentNames", "name"):
            value = item.get(key)
            if isinstance(value, list):
                raw_values.extend(value)
            elif value is not None:
                raw_values.append(value)

        names = []
        for raw in raw_values:
            if isinstance(raw, dict):
                raw = raw.get("name") or raw.get("displayName") or raw.get("id") or raw.get("slug")
            key = str(raw or "").strip()
            if not key:
                continue
            resolved = (
                self.augment_name_map.get(key)
                or self.augment_name_map.get(normalize_augment_name(key))
                or self.augment_name_map.get(Path(key).stem)
                or self.augment_name_map.get(normalize_augment_name(Path(key).stem))
            )
            if resolved:
                names.append(resolved)
            elif not key.isdigit() and len(key) > 1:
                names.append(key)
        return [name for name in dict.fromkeys(names) if name]

    def _extract_old_bundle(self, bundle_text: str) -> list[SynergyEntry]:
        if BUNDLE_INTERACTION_SECTION_MARKER not in bundle_text:
            return []
        payload = self._extract_interaction_payload(bundle_text)
        arrays = payload["arrays"]
        entries = []
        for mapping_name in ("manual_map", "community_map"):
            champion_map = payload.get(mapping_name) or {}
            for champion_slug, short_key in champion_map.items():
                array_literal = arrays.get(str(short_key))
                if not array_literal:
                    continue
                try:
                    items = self._parse_js_array_literal(array_literal)
                except Exception as exc:
                    logger.warning("解析旧 bundle 数组失败：champion=%s error=%s", champion_slug, _safe_exception_label(exc))
                    continue
                for item in items:
                    entry = self._entry_from_dict(item, fallback_slug=str(champion_slug))
                    if entry:
                        entries.append(entry)
        return entries

    def _extract_generic_js_objects(self, text: str) -> list[SynergyEntry]:
        entries = []
        if "hextech" not in text and "augment" not in text and "rating" not in text:
            return entries
        object_pattern = re.compile(r"\{[^{}]{0,1200}(?:hextechId|hextechIds|augmentId|augmentIds|rating|isOriginal)[^{}]{0,1200}\}")
        for match in object_pattern.finditer(text):
            literal = match.group(0)
            try:
                item = ast.literal_eval(self._convert_js_literal_to_python(literal))
            except Exception:
                continue
            if isinstance(item, dict):
                entry = self._entry_from_dict(item, fallback_slug="")
                if entry:
                    entries.append(entry)
        return entries

    def _extract_interaction_payload(self, bundle_text: str) -> dict:
        section_index = bundle_text.find(BUNDLE_INTERACTION_SECTION_MARKER)
        if section_index == -1:
            raise ValueError("未找到联动数据起始标记")
        section_index += len(BUNDLE_INTERACTION_SECTION_MARKER)

        manual_index = bundle_text.find("Tk={", section_index)
        community_index = bundle_text.find("RA={", section_index)
        if manual_index == -1 or community_index == -1:
            raise ValueError("未找到英雄映射对象")

        manual_literal, manual_object_end = self._extract_js_object_literal(bundle_text, manual_index + len("Tk="))
        community_literal, _ = self._extract_js_object_literal(bundle_text, community_index + len("RA="))

        stop_index = bundle_text.rfind("],RA={", manual_object_end, community_index)
        stop_index = stop_index + 1 if stop_index != -1 else community_index
        short_key_arrays = {}
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, section_index, manual_index))
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, manual_object_end, stop_index))
        if not short_key_arrays:
            raise ValueError("未找到联动数组定义")

        return {
            "arrays": short_key_arrays,
            "manual_map": self._parse_js_identifier_map(manual_literal),
            "community_map": self._parse_js_identifier_map(community_literal),
        }

    def _extract_js_object_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "{", "}")

    def _extract_js_array_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "[", "]")

    def _extract_balanced_literal(self, text: str, start_index: int, opener: str, closer: str) -> tuple[str, int]:
        if start_index < 0 or start_index >= len(text) or text[start_index] != opener:
            raise ValueError("字面量起始位置无效")
        depth = 0
        quote = None
        escaped = False
        i = start_index
        while i < len(text):
            char = text[i]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            else:
                if char in ('"', "'", "`"):
                    quote = char
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        return text[start_index : i + 1], i + 1
            i += 1
        raise ValueError("字面量未闭合")

    def _extract_named_array_assignments(self, text: str, start_index: int, stop_index: int) -> dict:
        assignments = {}
        pattern = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)=\[")
        cursor = start_index
        while cursor < stop_index:
            match = pattern.search(text, cursor, stop_index)
            if not match:
                break
            literal, cursor = self._extract_js_array_literal(text, match.end() - 1)
            assignments[match.group(1)] = literal
            if cursor < stop_index and text[cursor : cursor + 1] == ",":
                cursor += 1
        return assignments

    def _parse_js_identifier_map(self, literal: str) -> dict:
        body = literal.strip()
        if not body.startswith("{") or not body.endswith("}"):
            raise ValueError("英雄映射对象格式无效")
        mapping = {}
        pair_pattern = re.compile(r'''(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*([A-Za-z_$][A-Za-z0-9_$]*)''')
        for match in pair_pattern.finditer(body[1:-1]):
            key = match.group(1) or match.group(2) or match.group(3)
            value = match.group(4)
            if key:
                mapping[key] = value
        if not mapping:
            raise ValueError("英雄映射对象解析为空")
        return mapping

    def _parse_js_array_literal(self, literal: str) -> list:
        return ast.literal_eval(self._convert_js_literal_to_python(literal))

    def _convert_js_literal_to_python(self, literal: str) -> str:
        result = []
        i = 0
        simple_escapes = {"n": "\\n", "r": "\\r", "t": "\\t", "b": "\\b", "f": "\\f", "\\": "\\", '"': '"', "'": "'", "`": "`", "/": "/"}
        while i < len(literal):
            char = literal[i]
            if char in ('"', "'", "`"):
                quote = char
                i += 1
                chunks = []
                while i < len(literal):
                    current = literal[i]
                    if current == "\\":
                        i += 1
                        if i >= len(literal):
                            chunks.append("\\")
                            break
                        escaped = literal[i]
                        if escaped == "u" and i + 4 < len(literal):
                            hex_part = literal[i + 1 : i + 5]
                            if all(c in "0123456789abcdefABCDEF" for c in hex_part):
                                chunks.append(chr(int(hex_part, 16)))
                                i += 5
                                continue
                        chunks.append(simple_escapes.get(escaped, escaped))
                        i += 1
                        continue
                    if current == quote:
                        i += 1
                        break
                    chunks.append(current)
                    i += 1
                result.append(json.dumps("".join(chunks), ensure_ascii=False))
                continue
            if char == "!" and i + 1 < len(literal) and literal[i + 1] in "01":
                result.append("True" if literal[i + 1] == "0" else "False")
                i += 2
                continue
            if char.isalpha() or char in "_$":
                j = i + 1
                while j < len(literal) and (literal[j].isalnum() or literal[j] in "_$"):
                    j += 1
                token = literal[i:j]
                k = j
                while k < len(literal) and literal[k].isspace():
                    k += 1
                if k < len(literal) and literal[k] == ":":
                    result.append(json.dumps(token, ensure_ascii=False))
                    result.append(literal[j : k + 1])
                    i = k + 1
                    continue
                result.append({"null": "None", "true": "True", "false": "False", "undefined": "None"}.get(token, token))
                i = j
                continue
            result.append(char)
            i += 1
        return "".join(result)

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return normalize_slug(Path(tail).stem or tail)

    def _slug_from_path(self, path: tuple[str, ...]) -> str:
        for part in reversed(path):
            if not part.isdigit() and len(part) > 1:
                slug = normalize_slug(part)
                if slug in self.champion_lookup:
                    return slug
        return ""

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _dedupe_entries(entries_by_slug: dict[str, list[SynergyEntry]]) -> dict[str, list[SynergyEntry]]:
        result = {}
        for slug, entries in entries_by_slug.items():
            seen = set()
            unique = []
            for entry in entries:
                key = (tuple(entry.augment_names), entry.rating, entry.tag, entry.author, entry.content)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(entry)
            result[slug] = unique
        return result


class SynergyWriter:
    def __init__(self, core_info: dict[str, ChampionInfo]):
        self.core_info = core_info
        self.champion_lookup = build_champion_lookup(core_info)

    def build_payload(self, synergy_map: dict[str, list[SynergyEntry]]) -> dict:
        final_data = {}
        missing_synergy = []
        for champ_id, champ_info in self.core_info.items():
            synergies = self._find_synergies_for_champion(champ_info, synergy_map)
            if not synergies:
                missing_synergy.append(champ_info.name)
            final_data[champ_id] = {
                "id": champ_id,
                "name": champ_info.name,
                "title": champ_info.title,
                "en_name": champ_info.en_name,
                "aliases": champ_info.aliases,
                "synergies": [entry.to_compat_string() for entry in synergies],
                "synergy_items": [
                    {
                        "augment_names": entry.augment_names,
                        "tier": entry.tier,
                        "rating": entry.rating,
                        "tag": entry.tag,
                        "author": entry.author,
                        "is_original": entry.is_original,
                        "content": entry.content,
                        "upvotes": entry.upvotes,
                        "downvotes": entry.downvotes,
                    }
                    for entry in synergies
                ],
            }
        if missing_synergy:
            logger.warning("部分英雄暂无联动：count=%s", len(missing_synergy))
        return final_data

    def write(self, output_path: Path, payload: dict) -> None:
        lock_path = output_path.with_suffix(output_path.suffix + ".lock")
        with _output_file_lock(lock_path):
            _atomic_write_json(output_path, payload)

    def _find_synergies_for_champion(self, champ_info: ChampionInfo, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
        keys = [champ_info.slug, champ_info.en_name, champ_info.name, champ_info.title, champ_info.id, *champ_info.aliases]
        for key in keys:
            normalized = normalize_slug(key)
            if normalized in synergy_map:
                return synergy_map[normalized]
            name_key = normalize_name(key)
            if name_key in synergy_map:
                return synergy_map[name_key]
        return []


def build_augment_name_map_from_static() -> dict:
    name_map = {}

    def add_mapping(raw_key, raw_name):
        key = str(raw_key or "").strip()
        name = str(raw_name or "").strip()
        if not key or not name:
            return
        candidates = {key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)}
        for candidate in candidates:
            if candidate:
                name_map.setdefault(candidate, name)

    for filename in ("Augment_Apexlol_Map.json", "Augment_Icon_Manifest.json"):
        path = STATIC_DATA_PATH / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if filename == "Augment_Apexlol_Map.json" and isinstance(payload, dict):
            for raw_name, raw_slug in payload.items():
                add_mapping(raw_slug, raw_name)
                add_mapping(raw_name, raw_name)
        elif filename == "Augment_Icon_Manifest.json" and isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                filename_stem = Path(str(item.get("filename") or "")).stem
                add_mapping(name, name)
                add_mapping(filename_stem, name)

    if name_map:
        return name_map

    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        logger.warning("最新 runtime CSV 不存在，无法构建海克斯名称映射")
        return {}

    try:
        import pandas as pd

        df = pd.read_csv(latest_csv, usecols=["海克斯名称"])
        for _, row in df.iterrows():
            name = str(row.get("海克斯名称") or "").strip()
            if name:
                add_mapping(name, name)
    except Exception as exc:
        logger.warning("读取最新 CSV 构建海克斯名称映射失败：%s", _safe_exception_label(exc))
    return name_map


def main(*, dry_run: Optional[bool] = None, output_path: Optional[str] = None):
    started_at = time.time()
    dry_run = (os.getenv("APEX_DRY_RUN", "0").strip() == "1") if dry_run is None else bool(dry_run)
    logger.info("ApexLoL 协同抓取开始：dry_run=%s", dry_run)

    try:
        core_data = _load_json_file("Champion_Core_Data.json", "core_data")
        core_info = build_core_info(core_data)
        logger.info("核心数据加载成功：count=%s", len(core_info))
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        log_task_summary(logger, task="ApexLoL 协同抓取", started_at=started_at, success=False, detail=f"stage=core_data error={_safe_exception_label(exc)}")
        return None

    source = ApexSource()
    try:
        resources = source.discover_resources()
        if not resources:
            raise ValueError("Apex 页面和资源均不可用")
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(core_info),
            augment_name_map=build_augment_name_map_from_static(),
        )
        synergy_map = extractor.extract(resources)
        payload = SynergyWriter(core_info).build_payload(synergy_map)
        target_path = Path(output_path or build_synergy_data_path())
        if dry_run:
            log_task_summary(
                logger,
                task="ApexLoL 协同抓取",
                started_at=started_at,
                success=True,
                detail=f"dry_run=1 heroes={len(payload)} mapped={len(synergy_map)} resources={len(resources)}",
            )
        else:
            SynergyWriter(core_info).write(target_path, payload)
            log_task_summary(
                logger,
                task="ApexLoL 协同抓取",
                started_at=started_at,
                success=True,
                detail=f"heroes={len(payload)} mapped={len(synergy_map)} output={target_path.name}",
            )
        return {"resources": len(resources), "synergy_data": payload, "dry_run": dry_run}
    except Exception as exc:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail=f"stage=synergy_extract error={_safe_exception_label(exc)}",
        )
        logger.warning("ApexLoL 协同抓取失败，旧 Champion_Synergy.json 保持不变：%s", exc)
        return None
    finally:
        source.close()


if __name__ == "__main__":
    main()
