"""Web 服务运行时支撑层。

文件职责：
- 承载 Web 服务的长生命周期状态和请求热路径辅助逻辑
- 统一管理 LCU 轮询、CSV 监视、冷启动快照和资源缓存回退

核心输入：
- 本地 `data/static/version`、`data/static/assets` 与运行态缓存
- LCU 本地接口、远端快照接口和海克斯图标资源

核心输出：
- Web API 可直接消费的运行时数据、缓存结果和广播事件

主要依赖：
- `hextech.catalog.runtime_store`
- `hextech.core.refresh`
- `hextech.scraping.version_sync`
- `hextech.scraping.icon_resolver`

维护提醒：
- 这里不定义 FastAPI 路由，只提供路由层和启动壳依赖的运行时能力
- 涉及轮询频率、缓存 TTL 和资源回退策略的改动都应优先回归 Web 热路径

调用方: display.web.app、dev_checks; 关键依赖: pandas、psutil、requests。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import tempfile
import webbrowser
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse
from secrets import token_urlsafe

import pandas as pd
import psutil
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from hextech.client_context import ClientContextProvider
from hextech.data_snapshot import DataSnapshotClient
from hextech.catalog.runtime_store import (
    ensure_private_runtime_dir,
    ensure_runtime_profile_dir,
    build_runtime_profile_path,
    build_runtime_state_path,
    write_private_runtime_state_file,
)
from hextech.core.refresh import get_startup_status_file
from hextech.support.log_utils import ensure_utf8_stdio
from hextech.scraping.augment_catalog import find_augment_catalog_entry as find_augment_catalog_entry
from hextech.scraping.icon_resolver import (
    ensure_augment_icon_cached,
    find_existing_augment_asset_filename as find_existing_augment_asset_filename,
    is_safe_augment_icon_filename,
    is_safe_remote_icon_url,
    resolve_apexlol_hextech_icon_url,
)
from hextech.support.image_validation import is_valid_png_bytes, read_limited_response_bytes
from hextech.scraping.version_sync import (
    ASSET_DIR,
    BASE_DIR,
    BUNDLE_ROOT_DIR,
    STATIC_DATA_DIR,
    VERSION_FILE,
    load_champion_core_data,
)

ensure_utf8_stdio()

logger = logging.getLogger(__name__)

def _load_server_port() -> int:
    raw_port = str(os.getenv("HEXTECH_PORT", "8000")).strip()
    try:
        port = int(raw_port)
    except ValueError:
        return 8000
    return port if 1024 <= port <= 65535 else 8000


SERVER_PORT = _load_server_port()
WEB_PORT_FILE = build_runtime_state_path("web_server_port.txt")
BROWSER_PROFILE_DIR = build_runtime_profile_path("browser_profile")
AUTO_JUMP_ENABLED = True
HTTP_SESSION_COOKIE = "hextech_local_token"
REQUEST_TOKEN_HEADER = "x-hextech-token"

_managed_browser_process: Optional[subprocess.Popen] = None
_managed_browser_lock = threading.Lock()
_augment_cache_pending: Set[str] = set()
_augment_cache_pending_lock = threading.Lock()
_augment_cache_executor: ThreadPoolExecutor | None = None
_augment_cache_max_pending = 64
_snapshot_client = DataSnapshotClient()
_startup_status_file = get_startup_status_file()
_active_web_port = SERVER_PORT
_static_dir: Optional[str] = None
_assets_dir: Optional[str] = None
_champion_core_cache: Optional[dict] = None
_request_auth_token = token_urlsafe(24)

_SAFE_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_HERO_ID_RE = re.compile(r"^\d{1,6}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff .'\-]{1,64}$")
_lcu_warning_logged = False


def _executor_shutdown(executor: ThreadPoolExecutor | None) -> bool:
    return executor is None or bool(getattr(executor, "_shutdown", False))


def _executor_queue_size(executor: ThreadPoolExecutor | None) -> int:
    queue = getattr(executor, "_work_queue", None)
    qsize = getattr(queue, "qsize", None)
    if callable(qsize):
        try:
            return int(qsize())
        except Exception:
            return 0
    return 0


def _get_augment_cache_executor() -> ThreadPoolExecutor:
    global _augment_cache_executor
    if _executor_shutdown(_augment_cache_executor):
        _augment_cache_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="augment-cache")
    return _augment_cache_executor


def ensure_web_executors_started() -> None:
    _get_augment_cache_executor()


def shutdown_web_executors(*, wait: bool = True, cancel_futures: bool = True) -> None:
    global _augment_cache_executor
    for executor in (_augment_cache_executor,):
        if executor is not None and not _executor_shutdown(executor):
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)
    with _augment_cache_pending_lock:
        _augment_cache_pending.clear()
    _augment_cache_executor = None


def get_web_executor_health() -> dict:
    with _augment_cache_pending_lock:
        augment_pending = len(_augment_cache_pending)
    return {
        "augment_cache": {
            "shutdown": _executor_shutdown(_augment_cache_executor),
            "pending": augment_pending,
            "queue_depth": _executor_queue_size(_augment_cache_executor),
        },
        "hextech_preload": {
            "shutdown": True,
            "pending": 0,
            "queue_depth": 0,
            "owner": "data_service",
        },
    }


def get_request_auth_token() -> str:
    return _request_auth_token


def write_request_auth_token() -> None:
    """把本轮 Web token 写入私有 state 文件，供桌面 UI 读取 header。"""
    write_private_runtime_state_file("auth_token.txt", _request_auth_token)


def _get_resource_path(relative_path: str) -> str:
    candidates = [
        os.path.join(BUNDLE_ROOT_DIR, relative_path),
        os.path.join(BASE_DIR, relative_path),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def get_static_dir() -> str:
    global _static_dir
    if _static_dir is None:
        _static_dir = _get_resource_path("static")
        os.makedirs(_static_dir, exist_ok=True)
    return _static_dir


def get_assets_dir() -> str:
    global _assets_dir
    if _assets_dir is None:
        # 源码态与冻结态都使用 `_paths.ASSET_DIR` 作为可写图片事实源；
        # 打包内的 `assets` 只作为首启播种输入，不作为运行期写入目录。
        _assets_dir = ASSET_DIR
        os.makedirs(_assets_dir, exist_ok=True)
    return _assets_dir


def is_safe_png_asset_name(filename: str) -> bool:
    """校验海克斯图标文件名只包含安全字符，并固定为 png。"""
    return is_safe_augment_icon_filename(filename)


def is_allowed_local_origin(origin: str | None) -> bool:
    """校验浏览器来源是否来自本机页面。

    只接受当前 Web 端口上的本机页面；缺失 `Origin` 的请求一律拒绝。
    """
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = str(parsed.hostname or "").strip().lower()
    if host not in _SAFE_LOCAL_HOSTS:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return int(port) == int(get_active_web_port())


def get_active_web_port() -> int:
    return _active_web_port


def set_active_web_port(port: int) -> None:
    global _active_web_port
    _active_web_port = port


def write_active_web_port(port: int) -> None:
    write_private_runtime_state_file("web_server_port.txt", str(port))


def safe_join_under_dir(base_dir: str, filename: str) -> Optional[str]:
    local_path = os.path.join(base_dir, filename)
    real_requested = os.path.normcase(os.path.realpath(local_path))
    real_assets_dir = os.path.normcase(os.path.realpath(base_dir))
    if real_requested == real_assets_dir:
        return None
    if not real_requested.startswith(real_assets_dir + os.sep):
        return None
    return local_path


def is_safe_redirect_url(url: str) -> bool:
    return is_safe_remote_icon_url(url)


def resolve_remote_augment_icon_url(catalog_entry: Optional[dict], fallback_name: str) -> str:
    """解析海克斯图标的远端兜底地址。

    只保留显式远端地址与 apexlol 兜底，不再回退旧的 CDN 域名。
    """
    if catalog_entry:
        manifest_url = str(catalog_entry.get("icon_url", "")).strip()
        if is_safe_redirect_url(manifest_url):
            return manifest_url
        augment_name = str(catalog_entry.get("name", "")).strip() or fallback_name
    else:
        augment_name = fallback_name

    remote_url = resolve_apexlol_hextech_icon_url(augment_name, config_dir=STATIC_DATA_DIR)
    if remote_url and not remote_url.startswith("/assets/") and is_safe_redirect_url(remote_url):
        return remote_url
    return ""


def download_augment_icon_from_remote(augment_name: str, icon_filename: str) -> Optional[str]:
    """按文件名把远端海克斯图标下载到本地资源目录。"""
    safe_filename = os.path.basename(str(icon_filename or "").strip())
    if not is_safe_png_asset_name(safe_filename):
        logger.warning("已拒绝不安全的海克斯图标文件名：%s", icon_filename)
        return None

    remote_url = resolve_remote_augment_icon_url({"name": augment_name, "filename": safe_filename}, augment_name)
    if not remote_url:
        return None

    target_path = safe_join_under_dir(get_assets_dir(), safe_filename)
    if not target_path:
        logger.warning("已阻止图标缓存目录穿越：%s", safe_filename)
        return None
    fd, tmp_path = tempfile.mkstemp(prefix="augment-", suffix=".tmp", dir=os.path.dirname(target_path))
    os.close(fd)
    try:
        response = requests.get(remote_url, stream=True, timeout=15)
        if response.status_code != 200:
            return None
        content = read_limited_response_bytes(response)
        if content is None:
            return None
        if not is_valid_png_bytes(content):
            return None
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, target_path)
        return target_path
    except Exception:
        return None
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def queue_augment_icon_cache(icon_filename: str, augment_name: str = "") -> None:
    """把图标缓存任务放入后台线程，避免接口热路径阻塞。"""
    normalized = os.path.basename(str(icon_filename or "").strip())
    if not normalized or not is_safe_png_asset_name(normalized):
        return

    with _augment_cache_pending_lock:
        if normalized in _augment_cache_pending:
            return
        if len(_augment_cache_pending) >= _augment_cache_max_pending:
            logger.warning("海克斯图标缓存排队已达上限，拒绝追加：%s", normalized)
            return
        _augment_cache_pending.add(normalized)

    def _worker() -> None:
        try:
            cached_path = ensure_augment_icon_cached(normalized, asset_dir=get_assets_dir())
            if cached_path and os.path.exists(cached_path):
                return
            if augment_name:
                download_augment_icon_from_remote(augment_name, normalized)
        finally:
            with _augment_cache_pending_lock:
                _augment_cache_pending.discard(normalized)

    _get_augment_cache_executor().submit(_worker)


def request_background_refresh(force: bool = False, *, source: str = "api") -> dict:
    """兼容旧调用点；Web 无权发起刷新，调用方必须提交给 DataService。"""

    del force, source
    return {"accepted": False, "reason": "data_service_required"}


def _iter_browser_candidates() -> List[str]:
    configured = str(os.getenv("HEXTECH_BROWSER", "")).strip()
    candidates = []
    if configured:
        configured_path = shutil.which(configured) if not os.path.isabs(configured) else configured
        if configured_path and os.path.isfile(configured_path):
            candidates.append(configured_path)
        else:
            logger.warning("已忽略不合法的 HEXTECH_BROWSER 配置：%s", configured)
    candidates.extend(["msedge", "chrome", "brave"])
    resolved: List[str] = []
    for candidate in candidates:
        path = shutil.which(candidate)
        if path and path not in resolved:
            resolved.append(path)
    return resolved


def _terminate_managed_browser() -> bool:
    global _managed_browser_process

    proc = _managed_browser_process
    if proc is None:
        return False

    _managed_browser_process = None
    if proc.poll() is not None:
        return True

    try:
        parent = psutil.Process(proc.pid)
    except psutil.Error:
        return False

    try:
        children = parent.children(recursive=True)
    except psutil.Error:
        children = []

    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(children, timeout=2)

    try:
        parent.terminate()
        parent.wait(timeout=2)
    except psutil.TimeoutExpired:
        try:
            parent.kill()
        except psutil.Error:
            pass
    except psutil.Error:
        return False

    return True


def terminate_managed_browser() -> bool:
    """关闭由本进程启动的受管浏览器窗口。"""

    with _managed_browser_lock:
        return _terminate_managed_browser()


def is_safe_internal_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == "http" and str(parsed.hostname or "").strip() == "127.0.0.1"


def open_managed_browser(url: str, replace_existing: bool = False, *, allow_system_fallback: bool = True) -> bool:
    global _managed_browser_process

    ensure_runtime_profile_dir()
    ensure_private_runtime_dir(BROWSER_PROFILE_DIR)
    if not is_safe_internal_url(url):
        logger.warning("已拒绝启动非本地浏览器地址：%s", url)
        return False

    with _managed_browser_lock:
        existing = _managed_browser_process
        if existing is not None and existing.poll() is not None:
            _managed_browser_process = None
            existing = None

        if replace_existing and existing is not None:
            _terminate_managed_browser()

        for browser_path in _iter_browser_candidates():
            cmd = [
                browser_path,
                f"--app={url}",
                "--new-window",
                f"--user-data-dir={BROWSER_PROFILE_DIR}",
            ]
            try:
                _managed_browser_process = subprocess.Popen(cmd)
                logger.info("已启动受管浏览器窗口：%s", url)
                return True
            except OSError as exc:
                logger.debug("启动浏览器 %s 失败：%s", browser_path, exc)

    if allow_system_fallback:
        try:
            webbrowser.open(url)
            logger.info("已通过系统默认浏览器打开：%s", url)
            return True
        except Exception as exc:
            logger.warning("打开浏览器失败：%s", exc)
            return False
    return False


def request_open_managed_browser_async(url: str, replace_existing: bool = False) -> bool:
    """登记浏览器打开任务，避免把进程启动耗时压到 API 热路径。"""
    if not is_safe_internal_url(url):
        logger.warning("已拒绝异步启动非本地浏览器地址：%s", url)
        return False

    def _worker() -> None:
        try:
            open_managed_browser(url, replace_existing=replace_existing)
        except Exception:
            logger.exception("异步打开浏览器失败：%s", url)

    threading.Thread(
        target=_worker,
        daemon=True,
        name="managed-browser-open",
    ).start()
    return True


def build_detail_url(hero_id: str, hero_name: str, en_name: str) -> str:
    normalized_id = str(hero_id or "").strip()
    normalized_name = str(hero_name or "").strip()
    normalized_en_name = str(en_name or "").strip()
    if not _SAFE_HERO_ID_RE.fullmatch(normalized_id):
        raise ValueError("invalid_hero_id")
    if normalized_name and not _SAFE_NAME_RE.fullmatch(normalized_name):
        raise ValueError("invalid_hero_name")
    if normalized_en_name and not _SAFE_NAME_RE.fullmatch(normalized_en_name):
        raise ValueError("invalid_en_name")
    return (
        f"http://127.0.0.1:{get_active_web_port()}/detail.html"
        f"?hero={quote(normalized_name, safe='')}"
        f"&id={quote(normalized_id, safe='')}"
        f"&en={quote(normalized_en_name, safe='')}"
        f"&auto=1"
    )


def ensure_champion_cache() -> dict:
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as exc:
            logger.warning("英雄核心数据加载失败：%s", exc)
            _champion_core_cache = {}
    return _champion_core_cache


def get_champion_name(champ_id: str) -> str:
    cache = ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        return cache[champ_id_str].get("name", "")
    return ""


def get_champion_info(champ_id: str) -> Tuple[str, str]:
    cache = ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        data = cache[champ_id_str]
        return data.get("name", ""), data.get("en_name", "")
    return "", ""


def resolve_core_hero_record(query: str) -> Optional[dict]:
    normalized_query = str(query or "").strip().lower()
    if not normalized_query:
        return None

    cache = ensure_champion_cache()
    for champ_id, value in cache.items():
        hero_name = str(value.get("name", "")).strip()
        title = str(value.get("title", "")).strip()
        en_name = str(value.get("en_name", "")).strip()
        candidates = {
            hero_name.lower(),
            title.lower(),
            en_name.lower(),
            str(champ_id).strip().lower(),
        }
        if normalized_query in candidates:
            return {
                "heroId": str(champ_id),
                "heroName": hero_name,
                "title": title,
                "enName": en_name,
            }
    return None


def resolve_canonical_hero_name(query: str) -> str:
    record = resolve_core_hero_record(query)
    if record:
        return str(record.get("heroName", "")).strip()
    return str(query or "").strip()


def resolve_champion_id(query: str) -> str:
    raw_query = str(query or "").strip()
    if not raw_query:
        return ""
    if raw_query.isdigit():
        return raw_query

    record = resolve_core_hero_record(raw_query)
    if record:
        hero_id = str(record.get("heroId", "")).strip()
        if hero_id:
            return hero_id
    return ""


def get_ddragon_version() -> str:
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("无法读取 hero_version.txt，跳过 DDragon 回退。")
    return ""


def get_df() -> pd.DataFrame:
    """兼容旧查询入口；只把当前 generation 英雄榜投影为 DataFrame。"""

    try:
        return pd.DataFrame(_snapshot_client.get_champions())
    except Exception as exc:
        logger.debug("DataSnapshot 英雄榜尚不可用：%s", exc)
        return pd.DataFrame()


def _get_runtime_df_signature() -> Tuple[str, float]:
    status = _snapshot_client.status()
    return (str(status.get("generation_id") or ""), 0.0)


def clear_preloaded_hextech_payloads() -> None:
    """兼容旧生命周期调用；generation 客户端无需由 Web 清缓存。"""


def get_preloaded_hextech_payload(hero_name: str) -> Optional[dict]:
    """兼容旧名称；直接返回当前 generation 的不可变详情副本。"""

    canonical_name = resolve_canonical_hero_name(hero_name)
    if not canonical_name:
        return None
    return _snapshot_client.get_champion_detail(canonical_name)


def get_preload_hextech_status(hero_name: str) -> dict:
    canonical_name = resolve_canonical_hero_name(hero_name)
    if not canonical_name:
        return {"ready": False, "pending": False}
    try:
        snapshot_view = _snapshot_client.open_view()
        snapshot_status = snapshot_view.status()
        detail = snapshot_view.get_champion_detail(canonical_name)
    except Exception:
        snapshot_status = _snapshot_client.status()
        detail = None
    return {
        "ready": isinstance(detail, dict) and bool(detail.get("augments") or detail.get("comprehensive")),
        "pending": snapshot_status.get("state") == "unavailable",
        "generation_id": str(snapshot_status.get("generation_id") or ""),
    }


def request_preload_hextech_payload(hero_name: str) -> bool:
    return bool(get_preload_hextech_status(hero_name).get("ready"))


def request_preload_hextech_payload_async(hero_name: str) -> bool:
    """兼容旧名称；Web 不再创建数据任务，只探测已发布 generation。"""

    return request_preload_hextech_payload(hero_name)


def queue_preload_hextech_payloads(hero_names: List[str]) -> bool:
    return any(request_preload_hextech_payload(name) for name in hero_names)


async def get_df_with_refresh(timeout: float = 25.0) -> pd.DataFrame:
    """CSV 缺失时只等待已有产物出现；数据自愈由 Runtime Supervisor 发起。"""
    df = get_df()
    if not df.empty:
        return df

    deadline = time.time() + timeout
    while time.time() < deadline:
        df = get_df()
        if not df.empty:
            return df
        await asyncio.sleep(min(0.5, max(0.0, deadline - time.time())))
    return df


def get_stable_champion_catalog_df() -> pd.DataFrame:
    """读取 bundle 内稳定英雄目录，作为首页冷启动兜底数据源。"""
    core_data = load_champion_core_data()
    rows = []
    for champ_id, item in core_data.items():
        if not isinstance(item, dict):
            continue
        hero_name = str(item.get("name", "")).strip()
        if not hero_name:
            continue
        rows.append(
            {
                "英雄ID": str(champ_id).strip(),
                "英雄名称": hero_name,
                "英雄评级": "",
                "英雄胜率": 0.5,
                "英雄出场率": 0.001,
                "海克斯阶级": "",
                "海克斯名称": "",
                "海克斯胜率": 0.0,
                "海克斯出场率": 0.0,
                "胜率差": 0.0,
                "综合得分": 0.0,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def get_live_champion_snapshot_df(force_refresh: bool = False) -> pd.DataFrame:
    """兼容旧名称；返回当前 generation 英雄榜，不执行远端请求。"""

    del force_refresh
    return get_df()


def get_live_hextech_snapshot_df(hero_name: str, force_refresh: bool = False) -> pd.DataFrame:
    """兼容旧名称；返回当前 generation 单英雄详情，不执行远端请求。"""

    del force_refresh
    hero_name = resolve_canonical_hero_name(hero_name)
    if not hero_name:
        return pd.DataFrame()
    try:
        return pd.DataFrame(_snapshot_client.get_champion_augments(hero_name))
    except Exception as exc:
        logger.debug("DataSnapshot 单英雄详情尚不可用：hero=%s error=%s", hero_name, exc)
        return pd.DataFrame()


def get_synergy_data() -> dict:
    """兼容旧名称；联动数据与英雄统计来自同一 generation。"""
    try:
        return _snapshot_client.get_synergy_data()
    except Exception:
        return {}


def default_startup_status() -> dict:
    return {
        "first_run": False,
        "hero_ready": False,
        "hextech_ready": False,
        "synergy_ready": False,
        "augment_icons_prefetched": False,
        "in_progress_tasks": [],
        "last_error": "",
        "updated_at": "",
    }


def get_startup_status() -> dict:
    try:
        with open(_startup_status_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            merged = default_startup_status()
            merged.update(payload)
            return merged
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return default_startup_status()


class ConnectionManager:
    """WebSocket 连接池，负责广播实时事件。"""

    def __init__(self):
        self.active: List = []
        self._lock = asyncio.Lock()
        self._pending_connections = 0
        self.max_connections = 50

    async def connect(self, ws) -> None:
        async with self._lock:
            if len(self.active) + self._pending_connections >= self.max_connections:
                reject = True
            else:
                reject = False
                self._pending_connections += 1
        if reject:
            await ws.close(code=1013, reason="too_many_connections")
            return

        reserved = True
        try:
            await ws.accept()
            async with self._lock:
                self._pending_connections -= 1
                reserved = False
                self.active.append(ws)
        except BaseException:
            if reserved:
                async with self._lock:
                    self._pending_connections -= 1
            raise

    async def disconnect(self, ws) -> None:
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            snapshot = list(self.active)

        async def _send(ws):
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=1.0)
                return None
            except Exception:
                return ws

        dead = [ws for ws in await asyncio.gather(*(_send(ws) for ws in snapshot)) if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)


manager = ConnectionManager()


@dataclass
class LCUState:
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    selected_ids: list[str] = field(default_factory=list)
    bench_ids: list[str] = field(default_factory=list)
    teammate_ids: list[str] = field(default_factory=list)
    context_phase: str = "not_in_champ_select"
    connection_state: str = "disconnected"
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0
    state_version: int = 0
    updated_at: float = 0.0


_lcu_state = LCUState()
_lcu_state_lock = threading.RLock()
_client_context_provider = ClientContextProvider()


def get_live_state_payload() -> dict:
    with _lcu_state_lock:
        return {
            "champion_ids": sorted(_lcu_state.current_ids),
            "selected_champion_ids": list(_lcu_state.selected_ids),
            "bench_champion_ids": list(_lcu_state.bench_ids),
            "teammate_champion_ids": list(_lcu_state.teammate_ids),
            "context_phase": _lcu_state.context_phase,
            "context_connection_state": _lcu_state.connection_state,
            "local_champion_id": _lcu_state.local_champ_id,
            "local_champion_name": _lcu_state.local_champ_name,
            "state_version": _lcu_state.state_version,
            "updated_at": _lcu_state.updated_at,
        }


def _clean_champion_id(value) -> str:
    text = str(value or "").strip()
    return text if text and text != "0" else ""


def _append_unique_champion_id(target: list[str], value) -> None:
    champion_id = _clean_champion_id(value)
    if champion_id and champion_id not in target:
        target.append(champion_id)


def build_lcu_candidate_groups(payload: dict) -> dict[str, list[str]]:
    """从 LCU champ-select payload 生成 Web/桌面共用候选分组。"""

    from hextech.client_context import parse_client_context

    if not isinstance(payload, dict):
        return {"selected_champion_ids": [], "bench_champion_ids": []}
    return parse_client_context(payload).candidate_groups()


def _candidate_groups_to_id_set(candidate_groups: dict[str, list[str]]) -> set[str]:
    return {
        champion_id
        for key in ("selected_champion_ids", "bench_champion_ids")
        for champion_id in candidate_groups.get(key, [])
        if champion_id
    }


def _positive_champion_int(value) -> int | None:
    try:
        champion_id = int(value or 0)
    except (TypeError, ValueError):
        return None
    return champion_id if champion_id > 0 else None


def _extract_lcu_local_champion_id(payload: dict) -> int | None:
    from hextech.client_context import parse_client_context

    return _positive_champion_int(parse_client_context(payload).local_champion_id)


def _clear_lcu_local_champion_state() -> bool:
    """清空本机当前英雄；调用方必须持有 _lcu_state_lock。"""

    changed = _lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)
    _lcu_state.local_champ_id = None
    _lcu_state.local_champ_name = None
    if changed:
        _lcu_state.state_version += 1
        _lcu_state.updated_at = time.time()
    return changed


def _clear_lcu_candidate_state(*, clear_local: bool = False) -> bool:
    """清空 LCU 候选池；调用方必须持有 _lcu_state_lock。"""

    changed = bool(
        _lcu_state.current_ids
        or _lcu_state.selected_ids
        or _lcu_state.bench_ids
        or _lcu_state.teammate_ids
    )
    if clear_local:
        changed = changed or _lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)
        _lcu_state.local_champ_id = None
        _lcu_state.local_champ_name = None
    _lcu_state.current_ids = set()
    _lcu_state.selected_ids = []
    _lcu_state.bench_ids = []
    _lcu_state.teammate_ids = []
    if changed:
        _lcu_state.state_version += 1
        _lcu_state.updated_at = time.time()
    return changed


def _apply_client_context(context) -> tuple[dict[str, Any], bool]:
    """把 Provider 结果完整投影到 Web 状态，连接恢复也会推进版本。"""

    candidate_groups = context.candidate_groups(include_roles=True)
    available_ids = _candidate_groups_to_id_set(candidate_groups)
    with _lcu_state_lock:
        clear_local = context.connection_state == "disconnected" or (
            context.connection_state == "connected" and not candidate_groups.get("local_champion_id")
        )
        changed = (
            available_ids != _lcu_state.current_ids
            or candidate_groups["selected_champion_ids"] != _lcu_state.selected_ids
            or candidate_groups["bench_champion_ids"] != _lcu_state.bench_ids
            or candidate_groups["teammate_champion_ids"] != _lcu_state.teammate_ids
            or context.phase != _lcu_state.context_phase
            or context.connection_state != _lcu_state.connection_state
            or (clear_local and (_lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)))
        )
        _lcu_state.current_ids = available_ids.copy()
        _lcu_state.selected_ids = list(candidate_groups["selected_champion_ids"])
        _lcu_state.bench_ids = list(candidate_groups["bench_champion_ids"])
        _lcu_state.teammate_ids = list(candidate_groups["teammate_champion_ids"])
        _lcu_state.context_phase = context.phase
        _lcu_state.connection_state = context.connection_state
        if clear_local:
            _lcu_state.local_champ_id = None
            _lcu_state.local_champ_name = None
        if changed:
            _lcu_state.state_version += 1
            _lcu_state.updated_at = time.time()
    return candidate_groups, changed


def _create_lcu_session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[502, 503],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_lcu_session = _create_lcu_session()


def _scan_lcu_process() -> tuple:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] == "LeagueClientUx.exe":
                port, token = None, None
                for arg in proc.info["cmdline"] or []:
                    if arg.startswith("--app-port="):
                        port = arg.split("=")[1]
                    if arg.startswith("--remoting-auth-token="):
                        token = arg.split("=")[1]
                if port and token:
                    return port, token
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None, None


def _log_lcu_tls_warning_once() -> None:
    global _lcu_warning_logged
    if _lcu_warning_logged:
        return
    logger.info("LCU 本地 HTTPS 使用自签名证书，仅对 127.0.0.1 请求关闭证书校验。")
    _lcu_warning_logged = True


def _safe_exception_label(exc: Exception) -> str:
    return exc.__class__.__name__


def _get_lcu_session(url: str, headers: dict) -> requests.Response:
    """LCU 只监听本机自签名 HTTPS，这里局部关闭校验并局部抑制 warning。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        return _lcu_session.get(url, headers=headers, verify=False, timeout=3)


async def lcu_polling_loop() -> None:
    """持续轮询 LCU 选人会话，并把英雄可用集与锁定事件广播给前端。"""
    while True:
        try:
            with _lcu_state_lock:
                current_port = _lcu_state.port
                current_token = _lcu_state.token
            if not current_port:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    with _lcu_state_lock:
                        _lcu_state.port = port
                        _lcu_state.token = token
                    logger.info("已检测到 LCU 连接，端口=%s", port)
                    current_port = port
                    current_token = token
                else:
                    degraded_context = _client_context_provider.unavailable("client_not_found", source="web-lcu")
                    _apply_client_context(degraded_context)
                    if degraded_context.connection_state == "disconnected":
                        with _lcu_state_lock:
                            _clear_lcu_candidate_state(clear_local=True)
                    await asyncio.sleep(2)
                    continue

            if not current_token or not current_port:
                degraded_context = _client_context_provider.unavailable("credentials_missing", source="web-lcu")
                _apply_client_context(degraded_context)
                await asyncio.sleep(2)
                continue
            auth = base64.b64encode(f"riot:{current_token}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
            url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

            _log_lcu_tls_warning_once()
            res = await asyncio.to_thread(_get_lcu_session, url, headers)

            if res.status_code == 200:
                data = res.json()
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count = 0

                client_context = _client_context_provider.update(data, source="web-lcu")
                candidate_groups, state_changed = _apply_client_context(client_context)
                available_ids = _candidate_groups_to_id_set(candidate_groups)
                if state_changed:
                    await manager.broadcast(
                        {
                            "type": "champion_update",
                            "champion_ids": list(available_ids),
                            "selected_champion_ids": list(candidate_groups["selected_champion_ids"]),
                            "bench_champion_ids": list(candidate_groups["bench_champion_ids"]),
                            "local_champion_id": candidate_groups["local_champion_id"],
                            "teammate_champion_ids": list(candidate_groups["teammate_champion_ids"]),
                            "timestamp": time.time(),
                        }
                    )

                local_champion_id = _positive_champion_int(candidate_groups.get("local_champion_id"))

                if local_champion_id is not None:
                    with _lcu_state_lock:
                        prev_champ_id = _lcu_state.local_champ_id
                        hero_name = _lcu_state.local_champ_name or ""
                    if prev_champ_id != local_champion_id:
                        with _lcu_state_lock:
                            _lcu_state.local_champ_id = local_champion_id
                            _lcu_state.state_version += 1
                            _lcu_state.updated_at = time.time()
                        hero_name, en_name = get_champion_info(str(local_champion_id))
                        with _lcu_state_lock:
                            _lcu_state.local_champ_name = hero_name
                            _lcu_state.updated_at = time.time()
                        logger.info("LCU 已锁定英雄：%s (ID=%s)", hero_name, local_champion_id)
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast(
                                {
                                    "type": "local_player_locked",
                                    "champion_id": local_champion_id,
                                    "hero_name": hero_name,
                                    "en_name": en_name,
                                    "detail_first": True,
                                }
                            )

                else:
                    with _lcu_state_lock:
                        _clear_lcu_local_champion_state()
            elif res.status_code == 404:
                context = _client_context_provider.not_in_champ_select(source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count += 1
                    _clear_lcu_candidate_state(clear_local=True)
                    consecutive_404_count = _lcu_state.consecutive_404_count
                    _lcu_state.context_phase = "not_in_champ_select"
                    _lcu_state.connection_state = "connected"
                if consecutive_404_count >= 5:
                    logger.warning("LCU 连续返回 404 五次，重置连接状态（count=%s）", consecutive_404_count)
                    with _lcu_state_lock:
                        _lcu_state.port = None
                        _lcu_state.token = None
                        _lcu_state.consecutive_404_count = 0
                        _clear_lcu_candidate_state(clear_local=True)
            elif res.status_code in (401, 403):
                logger.warning("LCU token 失效或未授权（401/403），重置连接状态。")
                context = _client_context_provider.unavailable("authorization_failed", source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.port = None
                    _lcu_state.token = None
                    if context.connection_state == "disconnected":
                        _clear_lcu_candidate_state(clear_local=True)
            else:
                logger.warning("LCU 响应异常状态码=%s，重置连接状态。", res.status_code)
                context = _client_context_provider.unavailable(f"http_{res.status_code}", source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.port = None
                    if context.connection_state == "disconnected":
                        _clear_lcu_candidate_state(clear_local=True)
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "LCU 请求异常，已重置连接状态：error_type=%s endpoint=127.0.0.1",
                _safe_exception_label(exc),
            )
            degraded_context = _client_context_provider.unavailable("request_failed", source="web-lcu")
            _apply_client_context(degraded_context)
            with _lcu_state_lock:
                _lcu_state.port = None
                _lcu_state.token = None
                _lcu_state.connection_state = degraded_context.connection_state
                _lcu_state.context_phase = degraded_context.phase
                if degraded_context.connection_state == "disconnected":
                    _clear_lcu_candidate_state(clear_local=True)
        except Exception as exc:
            logger.warning(
                "LCU 轮询异常：error_type=%s context=local_polling_loop",
                _safe_exception_label(exc),
            )
            degraded_context = _client_context_provider.unavailable("unexpected_error", source="web-lcu")
            _apply_client_context(degraded_context)
            with _lcu_state_lock:
                if degraded_context.connection_state == "disconnected":
                    _clear_lcu_candidate_state(clear_local=True)

        await asyncio.sleep(1.5)


async def csv_watcher_loop() -> None:
    """监视 DataService generation，并在整代切换后广播数据刷新事件。"""

    previous_generation_id = ""
    while True:
        try:
            status = _snapshot_client.status()
            generation_id = str(status.get("generation_id") or "")
            if generation_id and previous_generation_id and generation_id != previous_generation_id:
                logger.info("DataService generation 已切换：generation_id=%s", generation_id)
                await manager.broadcast({"type": "data_updated", "generation_id": generation_id})
            previous_generation_id = generation_id or previous_generation_id
        except Exception as exc:
            logger.warning("DataService generation 监视器错误：%s", exc)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_app):
    """Web 生命周期钩子。

    只创建 LCU 与 generation 监视两条长生命周期任务；刷新由 DataService 唯一发起。
    """
    task1 = asyncio.create_task(lcu_polling_loop())
    task2 = asyncio.create_task(csv_watcher_loop())
    yield
    task1.cancel()
    task2.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass
    try:
        await task2
    except asyncio.CancelledError:
        pass
    shutdown_web_executors(wait=False)


def find_available_port(start_port: int = 8000, max_attempts: int = 50) -> int:
    import socket

    for port_offset in range(max_attempts):
        port = start_port + port_offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"未能在端口范围 {start_port}-{start_port + max_attempts - 1} 找到可用端口")


def maybe_open_browser(port: int) -> None:
    if os.getenv("HEXTECH_OPEN_BROWSER", "1").lower() in {"0", "false", "no"}:
        return
    open_managed_browser(f"http://127.0.0.1:{port}", replace_existing=True)
