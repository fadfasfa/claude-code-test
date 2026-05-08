"""海克斯联动数据抓取器。"""

import logging
import json
import os
import random
import sys
import time
import tempfile
import re
import ast
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from processing.runtime_store import build_synergy_data_path, get_latest_csv
from scraping.version_sync import STATIC_DATA_DIR
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse
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
STATIC_DATA_PATH = Path(STATIC_DATA_DIR)
ALLOWED_STATIC_DATA_FILES = {
    "Champion_Core_Data.json",
}
MAX_STATIC_DATA_FILE_SIZE = 10 * 1024 * 1024
MAX_FETCH_RETRIES = 1
REQUEST_TIMEOUT_SECONDS = 6
RETRY_BACKOFF_FACTOR = 0.5
THREAD_POOL_WORKERS = 8
THREAD_POOL_TIMEOUT_SECONDS = 28
OUTPUT_LOCK_TIMEOUT_SECONDS = 5
OUTPUT_LOCK_POLL_INTERVAL_SECONDS = 0.2
BUNDLE_INTERACTION_SECTION_MARKER = "fx={manual:gx},"
BUNDLE_INTERACTION_OBJECT_MARKER = "],Tk={"
BUNDLE_COMMUNITY_OBJECT_MARKER = "],RA={"
BUNDLE_APP_JS_PATTERN = re.compile(r'/assets/app\.[^"\']+\.js')
SYNERGY_TAG_LABELS = {
    "Synergy": "强力联动",
    "Trap": "陷阱",
    "Fun": "娱乐",
    "Bug": "缺陷",
}
TIER_LABELS = {
    "Prismatic": "棱彩",
    "Gold": "黄金",
    "Silver": "白银",
}

# 日志配置。
install_summary_logging(
    level=logging.INFO,
    fmt='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# 常见桌面浏览器请求标识池。
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    # 随机选择请求标识。
    return random.choice(USER_AGENTS)


def normalize_name(name_str: str) -> str:
    # 规范化英雄名称。
    if not name_str:
        return ""
    return name_str.replace(" ", "").replace("-", "").replace("'", "").replace(".", "").lower()


def _sanitize_url_for_log(url: str) -> str:
    # 日志中仅保留到路径级别，隐藏查询参数和 fragment。
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:200]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
            "",
        )
    )


def _resolve_static_data_path(filename: str) -> Path:
    # 将配置文件限制在固定白名单内，避免路径穿越。
    base_name = os.path.basename(filename)
    if base_name != filename or filename not in ALLOWED_STATIC_DATA_FILES:
        raise ValueError(f"不允许访问的配置文件：{filename}")

    resolved = (STATIC_DATA_PATH / filename).resolve()
    if STATIC_DATA_PATH.resolve() not in resolved.parents:
        raise ValueError(f"配置文件路径越界：{filename}")
    return resolved


def _load_json_file(filename: str, expected_kind: str) -> dict:
    # 读取受限配置文件并做基本结构校验。
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
    elif expected_kind == "aliases":
        for alias_name, alias_values in data.items():
            if not isinstance(alias_name, str) or not isinstance(alias_values, list):
                raise ValueError(f"{expected_kind} 配置内容格式错误：{filename}")

    return data


@contextmanager
def _output_file_lock(lock_path: Path, timeout_seconds: int = OUTPUT_LOCK_TIMEOUT_SECONDS):
    # 用锁文件串行化输出，避免并发实例同时写入。
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
    # 先写临时文件，再原子替换，避免中断导致 JSON 损坏。
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


def _safe_exception_label(exc: Exception) -> str:
    return exc.__class__.__name__


class ApexSpider:
    # 轻量级爬虫类，支持线程池并发抓取静态页面。

    def __init__(self):
        # 初始化爬虫会话和重试机制。
        self.base_url = os.environ.get("APEX_BASE_URL", "https://apexlol.info/zh").rstrip("/")
        parsed_base = urlparse(self.base_url)
        if parsed_base.scheme != "https" or not parsed_base.netloc:
            raise ValueError("APEX_BASE_URL 必须是有效的 https URL")
        self.allowed_netloc = parsed_base.netloc
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": get_random_user_agent()
        })

        logger.info(f"ApexSpider 初始化完成，User-Agent: {self.session.headers['User-Agent'][:50]}...，单层重试已启用")

    def _sanitize_log_url(self, url: str) -> str:
        return _sanitize_url_for_log(url)

    def _is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == self.allowed_netloc
        )

    def _build_allowed_detail_url(self, href: str) -> Optional[str]:
        candidate = urljoin(f"{self.base_url}/", href.strip())
        if not self._is_allowed_url(candidate):
            logger.warning(f"跳过非白名单链接：{self._sanitize_log_url(candidate)}")
            return None
        parsed = urlparse(candidate)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _discover_bundle_app_js_path(self) -> str:
        homepage_html = self.fetch_page(self.base_url)
        if homepage_html is None:
            raise ValueError("首页加载失败，无法发现 bundle")
        match = BUNDLE_APP_JS_PATTERN.search(homepage_html)
        if not match:
            raise ValueError("首页未发现 app bundle 路径")
        return match.group(0)

    def fetch_page(self, url: str) -> Optional[str]:
        # 获取页面内容，失败返回 None。
        retryable_status_codes = {429, 500, 502, 503, 504}

        if not self._is_allowed_url(url):
            logger.warning(f"拒绝非白名单请求：{self._sanitize_log_url(url)}")
            return None

        for attempt in range(MAX_FETCH_RETRIES + 1):
            try:
                logger.info(
                    f"正在加载页面：{self._sanitize_log_url(url)} "
                    f"(尝试 {attempt + 1}/{MAX_FETCH_RETRIES + 1})"
                )
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                response.encoding = 'utf-8'

                if response.status_code == 200:
                    return response.text
                elif response.status_code in retryable_status_codes and attempt < MAX_FETCH_RETRIES:
                    delay = RETRY_BACKOFF_FACTOR * (2 ** attempt)
                    logger.warning(
                        f"页面返回可重试状态码：{response.status_code}, "
                        f"URL: {self._sanitize_log_url(url)}, 将在 {delay:.2f} 秒后重试..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"页面返回状态码异常：{response.status_code}, "
                        f"URL: {self._sanitize_log_url(url)}"
                    )
                    return None

            except requests.Timeout:
                if attempt < MAX_FETCH_RETRIES:
                    delay = RETRY_BACKOFF_FACTOR * (2 ** attempt)
                    logger.warning(
                        f"页面加载超时 - URL: {self._sanitize_log_url(url)}, "
                        f"将在 {delay:.2f} 秒后重试..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"页面加载超时 - URL: {self._sanitize_log_url(url)}")
                    return None
            except requests.RequestException as e:
                if attempt < MAX_FETCH_RETRIES:
                    delay = RETRY_BACKOFF_FACTOR * (2 ** attempt)
                    logger.warning(
                        f"页面加载失败 - URL: {self._sanitize_log_url(url)}, "
                        f"错误：{_safe_exception_label(e)}, 将在 {delay:.2f} 秒后重试..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"页面加载失败 - URL: {self._sanitize_log_url(url)}, "
                        f"错误：{_safe_exception_label(e)}"
                    )
                    return None
            except Exception as e:
                logger.error(
                    f"页面加载异常 - URL: {self._sanitize_log_url(url)}, "
                    f"错误：{_safe_exception_label(e)}"
                )
                return None

    def crawl_champion_list(self) -> dict:
        # 爬取英雄列表并返回名称和详情页地址。
        url = f"{self.base_url}/champions"

        result = {
            "success": False,
            "url": url,
            "champions": [],
            "error": None
        }

        try:
            html = self.fetch_page(url)
            if html is None:
                result["error"] = "页面加载失败"
                return result

            soup = BeautifulSoup(html, 'html.parser')

            champ_cards = soup.select('.champ-card')

            champions = []
            for card in champ_cards:
                try:
                    name_elem = card.select_one('.name')
                    if not name_elem:
                        continue

                    name = name_elem.get_text(strip=True)

                    href = card.get('href')
                    if href:
                        full_url = self._build_allowed_detail_url(href)
                        if full_url:
                            champions.append({"name": name, "url": full_url})
                except Exception as e:
                    logger.warning(f"单个英雄卡片提取失败：{_safe_exception_label(e)}")
                    continue

            if champions:
                result["champions"] = champions
                result["success"] = True
            else:
                logger.warning("未找到匹配的英雄元素")

        except Exception as e:
            logger.error(
                f"爬虫执行异常 - URL: {self._sanitize_log_url(url)}, "
                f"错误：{_safe_exception_label(e)}"
            )
            result["error"] = "英雄列表解析异常"

        return result

    def build_augment_name_map_from_latest_csv(self) -> dict:
        # 从最新 runtime CSV 构建 hextechId 到中文海克斯名的映射。
        latest_csv = get_latest_csv()
        if not latest_csv or not os.path.exists(latest_csv):
            logger.warning("最新 runtime CSV 不存在，无法构建海克斯名称映射")
            return {}

        try:
            import pandas as pd
        except Exception as e:
            logger.warning(f"pandas 不可用，跳过海克斯名称映射：{_safe_exception_label(e)}")
            return {}

        try:
            df = pd.read_csv(latest_csv, usecols=["英雄 ID", "海克斯名称"])
        except Exception as e:
            logger.warning(f"读取最新 CSV 关键列失败：{_safe_exception_label(e)}")
            return {}

        name_map = {}
        for _, row in df.iterrows():
            raw_id = row.get("英雄 ID")
            raw_name = row.get("海克斯名称")
            if raw_name is None:
                continue
            name = str(raw_name).strip()
            if not name:
                continue
            hextech_id = str(raw_id).strip()
            if hextech_id and hextech_id.lower() != "nan":
                name_map.setdefault(hextech_id, name)
        return name_map

    def _extract_js_object_literal(self, text: str, start_index: int) -> tuple[str, int]:
        if start_index < 0 or start_index >= len(text) or text[start_index] != "{":
            raise ValueError("对象字面量起始位置无效")
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
                if char in ('"', "'", '`'):
                    quote = char
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start_index:i + 1], i + 1
            i += 1
        raise ValueError("对象字面量未闭合")

    def _extract_js_array_literal(self, text: str, start_index: int) -> tuple[str, int]:
        if start_index < 0 or start_index >= len(text) or text[start_index] != "[":
            raise ValueError("数组字面量起始位置无效")
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
                if char in ('"', "'", '`'):
                    quote = char
                elif char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                    if depth == 0:
                        return text[start_index:i + 1], i + 1
            i += 1
        raise ValueError("数组字面量未闭合")

    def _extract_named_array_assignments(self, text: str, start_index: int, stop_index: int) -> dict:
        assignments = {}
        pattern = re.compile(r'([A-Za-z_$][A-Za-z0-9_$]*)=\[')
        cursor = start_index
        while cursor < stop_index:
            match = pattern.search(text, cursor, stop_index)
            if not match:
                break
            array_start = match.end() - 1
            literal, cursor = self._extract_js_array_literal(text, array_start)
            assignments[match.group(1)] = literal
            if cursor < stop_index and text[cursor:cursor + 1] == ',':
                cursor += 1
        return assignments

    def _parse_js_identifier_map(self, literal: str) -> dict:
        body = literal.strip()
        if not body.startswith('{') or not body.endswith('}'):
            raise ValueError("英雄映射对象格式无效")
        body = body[1:-1]
        mapping = {}
        pair_pattern = re.compile(r'''(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*([A-Za-z_$][A-Za-z0-9_$]*)''')
        for match in pair_pattern.finditer(body):
            key = match.group(1) or match.group(2) or match.group(3)
            value = match.group(4)
            if key:
                mapping[key] = value
        if not mapping:
            raise ValueError("英雄映射对象解析为空")
        return mapping

    def _convert_js_literal_to_python(self, literal: str) -> str:
        result = []
        i = 0
        length = len(literal)
        simple_escapes = {
            "n": "\\n",
            "r": "\\r",
            "t": "\\t",
            "b": "\\b",
            "f": "\\f",
            "\\": "\\",
            '"': '"',
            "'": "'",
            "`": "`",
            "/": "/",
        }

        while i < length:
            char = literal[i]
            if char in ('"', "'", '`'):
                quote = char
                i += 1
                chunks = []
                while i < length:
                    current = literal[i]
                    if current == "\\":
                        i += 1
                        if i >= length:
                            chunks.append("\\")
                            break
                        escaped = literal[i]
                        if escaped == "u" and i + 4 < length:
                            hex_part = literal[i + 1:i + 5]
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

            if char == "!" and i + 1 < length and literal[i + 1] in "01":
                result.append("True" if literal[i + 1] == "0" else "False")
                i += 2
                continue

            if char.isalpha() or char in "_$":
                j = i + 1
                while j < length and (literal[j].isalnum() or literal[j] in "_$"):
                    j += 1
                token = literal[i:j]
                k = j
                while k < length and literal[k].isspace():
                    k += 1
                if k < length and literal[k] == ":":
                    result.append(json.dumps(token, ensure_ascii=False))
                    result.append(literal[j:k + 1])
                    i = k + 1
                    continue
                if token == "null":
                    result.append("None")
                elif token == "true":
                    result.append("True")
                elif token == "false":
                    result.append("False")
                elif token == "undefined":
                    result.append("None")
                else:
                    result.append(token)
                i = j
                continue

            result.append(char)
            i += 1

        return "".join(result)

    def _parse_js_array_literal(self, literal: str) -> list:
        python_literal = self._convert_js_literal_to_python(literal)
        return ast.literal_eval(python_literal)

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

        short_key_arrays = {}
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, section_index, manual_index))
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, manual_object_end, bundle_text.rfind("],RA={", manual_object_end, community_index) + 1 if bundle_text.rfind("],RA={", manual_object_end, community_index) != -1 else community_index))
        if not short_key_arrays:
            raise ValueError("未找到联动数组定义")

        manual_map = self._parse_js_identifier_map(manual_literal)
        community_map = self._parse_js_identifier_map(community_literal)
        return {
            "arrays": short_key_arrays,
            "manual_map": manual_map,
            "community_map": community_map,
        }

    def _resolve_augment_name(selfself, item: dict, augment_name_map: dict) -> Optional[str]:
        hextech_id = item.get("hextechId")
        if hextech_id is not None:
            key = str(hextech_id).strip()
            if key:
                resolved = augment_name_map.get(key)
                if resolved:
                    return resolved
        hextech_ids = item.get("hextechIds") or []
        if isinstance(hextech_ids, str):
            hextech_ids = [hextech_ids]
        names = []
        for raw_id in hextech_ids:
            key = str(raw_id).strip()
            if not key:
                continue
            resolved = augment_name_map.get(key)
            if resolved:
                names.append(resolved)
        if names:
            return ", ".join(dict.fromkeys(names))
        return None

    def _stringify_synergy_entry(self, item: dict, augment_name_map: dict) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not tags:
            return None
        tag_label = next((SYNERGY_TAG_LABELS.get(tag) for tag in tags if tag in SYNERGY_TAG_LABELS), None)
        if not tag_label:
            tag_label = "强力联动"
        augment_name = self._resolve_augment_name(item, augment_name_map)
        if not augment_name:
            return None
        rating = str(item.get("rating") or "").strip() or "未知"
        author = str(item.get("author") or item.get("contributor") or "ApexLoL").strip() or "ApexLoL"
        note = item.get("note") or {}
        content = str(note.get("zh") or "").replace("\r", " ").replace("\n", " ").strip()
        if not content:
            return None
        originality = "原创" if bool(item.get("isOriginal")) else "非原创"
        return " | ".join([
            augment_name,
            "黄金",
            f"评分 {rating}",
            tag_label,
            rating,
            author,
            originality,
            content,
        ])

    def extract_hextech_synergies(self) -> dict:
        bundle_url = urljoin(f"{self.base_url}/", self._discover_bundle_app_js_path())
        bundle_text = self.fetch_page(bundle_url)
        if bundle_text is None:
            raise ValueError("联动 bundle 拉取失败")
        payload = self._extract_interaction_payload(bundle_text)
        augment_name_map = self.build_augment_name_map_from_latest_csv()
        arrays = payload["arrays"]
        result = {}
        for mapping_name in ("manual_map", "community_map"):
            champion_map = payload.get(mapping_name) or {}
            for champion_slug, short_key in champion_map.items():
                array_literal = arrays.get(str(short_key))
                if not array_literal:
                    continue
                try:
                    items = self._parse_js_array_literal(array_literal)
                except Exception as e:
                    logger.warning(
                        "解析英雄联动数组失败：champion=%s key=%s error=%s",
                        champion_slug,
                        short_key,
                        _safe_exception_label(e),
                    )
                    continue
                for item in items:
                    synergy_text = self._stringify_synergy_entry(item, augment_name_map)
                    if not synergy_text:
                        continue
                    result.setdefault(champion_slug, []).append(synergy_text)
        if not result:
            raise ValueError("联动 bundle 解析结果为空")
        return result


def main():
    # 主函数入口
    started_at = time.time()
    logger.info("ApexLoL 协同抓取开始")

    # 创建爬虫实例
    spider = ApexSpider()

    # 爬取英雄列表
    champion_result = spider.crawl_champion_list()

    if champion_result["success"]:
        logger.info("英雄列表抓取成功：count=%s", len(champion_result["champions"]))
    else:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail=f"stage=champion_list error={champion_result.get('error')}",
        )
        return

    # 加载本地配置文件
    try:
        core_data = _load_json_file("Champion_Core_Data.json", "core_data")
        logger.info("核心数据加载成功：count=%s", len(core_data))
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as e:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail=f"stage=core_data error={_safe_exception_label(e)}",
        )
        return
    except Exception as e:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail=f"stage=core_data error={_safe_exception_label(e)}",
        )
        return

    # 构建输出用的完整数据字典，包含核心字段和别名列表
    core_info_dict = {}
    for champ_id, champ_info in core_data.items():
        name = champ_info.get("name")
        if name:
            core_info_dict[champ_id] = {
                "id": champ_id,
                "name": name,
                "title": champ_info.get("title", ""),
                "en_name": champ_info.get("en_name", ""),
                "aliases": champ_info.get("aliases", [])
            }

    # 构建网页名称匹配索引，统一纳入名称、称号、英文名和别名
    search_index = {}
    for champ_id, champ_info in core_info_dict.items():
        # 将名称、称号、英文名和别名加入搜索索引
        names_to_index = [
            champ_info["name"],
            champ_info["title"],
            champ_info["en_name"],
            *champ_info.get("aliases", [])
        ]

        for name_field in names_to_index:
            if name_field:
                normalized = normalize_name(name_field)
                if normalized:
                    search_index[normalized] = champ_id

    final_data = {}
    champions = champion_result.get("champions", [])
    if champions:
        task_map = {}
        skipped_names = []

        for champ in champions:
            champ_name = champ["name"]
            champion_slug = champ.get("url", "").rstrip("/").split("/")[-1]
            normalized_champ_name = normalize_name(champ_name)
            champ_id = search_index.get(normalized_champ_name)
            if not champ_id and champion_slug:
                champ_id = search_index.get(normalize_name(champion_slug))
            if not champ_id:
                skipped_names.append(champ_name)
                continue
            core_info = core_info_dict[champ_id]
            task_map[champion_slug] = {
                "name": core_info["name"],
                "id": champ_id,
                "title": core_info["title"],
                "en_name": core_info["en_name"],
                "aliases": core_info["aliases"],
            }

        if skipped_names:
            logger.warning("协同抓取存在未匹配英雄：count=%s", len(skipped_names))

        try:
            synergy_map = spider.extract_hextech_synergies()
        except Exception as e:
            log_task_summary(
                logger,
                task="ApexLoL 协同抓取",
                started_at=started_at,
                success=False,
                detail=f"stage=synergy_bundle error={_safe_exception_label(e)}",
            )
            return

        missing_synergy = []
        for champion_slug, champ_info in task_map.items():
            synergies = synergy_map.get(champion_slug, [])
            if not synergies:
                missing_synergy.append(champ_info["name"])
            champ_id = champ_info["id"]
            final_data[champ_id] = {
                "id": champ_id,
                "name": champ_info["name"],
                "title": champ_info["title"],
                "en_name": champ_info["en_name"],
                "aliases": champ_info["aliases"],
                "synergies": synergies,
            }

        if missing_synergy:
            logger.warning("部分英雄暂无联动：count=%s", len(missing_synergy))

        output_path = Path(build_synergy_data_path())
        lock_path = output_path.with_suffix(output_path.suffix + ".lock")
        with _output_file_lock(lock_path):
            _atomic_write_json(output_path, final_data)
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=True,
            detail=f"heroes={len(final_data)} mapped={len(synergy_map)} output={output_path.name}",
        )
    else:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail="stage=champion_list error=empty_result",
        )

    return {
        "champions": champion_result,
        "hextech_data": final_data
    }


