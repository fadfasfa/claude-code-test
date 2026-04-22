"""Web 服务运行时支撑层。

文件职责：
- 承载 Web 服务的长生命周期状态和请求热路径辅助逻辑
- 统一管理 LCU 轮询、CSV 监视、冷启动快照和资源缓存回退

核心输入：
- 本地 `config/` 与 `assets/` 目录
- LCU 本地接口、远端快照接口和海克斯图标资源

核心输出：
- Web API 可直接消费的运行时数据、缓存结果和广播事件

主要依赖：
- `processing.runtime_store`
- `processing.orchestrator`
- `scraping.version_sync`
- `scraping.icon_resolver`

维护提醒：
- 这里不定义 FastAPI 路由，只提供路由层和启动壳依赖的运行时能力
- 涉及轮询频率、缓存 TTL 和资源回退策略的改动都应优先回归 Web 热路径
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
import sys
import threading
import time
import tempfile
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Set, Tuple
from urllib.parse import quote, urlparse
from secrets import token_urlsafe

import pandas as pd
import psutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from processing.view_adapter import process_hextechs_data
from processing.runtime_store import CachedDataFrameLoader, get_latest_csv
from scraping.augment_catalog import find_augment_catalog_entry
from scraping.full_hextech_scraper import _clean_augment_text, extract_champion_stats, fetch_with_retry
from scraping.icon_resolver import (
    ensure_augment_icon_cached,
    find_existing_augment_asset_filename,
    resolve_apexlol_hextech_icon_url,
)
from scraping.version_sync import (
    BASE_DIR,
    CONFIG_DIR,
    RESOURCE_DIR,
    HEXTECH_AUGMENT_METADATA_URLS,
    HEXTECH_CHAMPION_STATS_URLS,
    build_hextech_detail_urls,
    get_advanced_session,
    load_augment_map,
    load_champion_core_data,
)
from processing.orchestrator import get_startup_status_file, refresh_backend_data
from tools.log_utils import ensure_utf8_stdio

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
WEB_PORT_FILE = os.path.join(CONFIG_DIR, "web_server_port.txt")
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")
BROWSER_PROFILE_DIR = os.path.join(CONFIG_DIR, "browser_profile")
AUTO_JUMP_ENABLED = True
HTTP_SESSION_COOKIE = "hextech_local_token"
REQUEST_TOKEN_HEADER = "x-hextech-token"

_managed_browser_process: Optional[subprocess.Popen] = None
_managed_browser_lock = threading.Lock()
_augment_cache_pending: Set[str] = set()
_augment_cache_pending_lock = threading.Lock()
_augment_cache_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="augment-cache")
_augment_cache_max_pending = 64
_csv_loader = CachedDataFrameLoader(get_latest_csv)
_startup_status_file = get_startup_status_file()
_active_web_port = SERVER_PORT
_static_dir: Optional[str] = None
_assets_dir: Optional[str] = None
_champion_core_cache: Optional[dict] = None
_background_refresh_lock = threading.Lock()
_background_refresh_inflight = False
_preloaded_hextech_lock = threading.Lock()
_preloaded_hextech_payloads: dict[str, dict] = {}
_preloaded_hextech_pending: Set[str] = set()
_preloaded_hextech_signature: Tuple[str, float] = ("", 0.0)
_preloaded_hextech_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hextech-preload")
_request_auth_token = token_urlsafe(24)

_SAFE_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_SAFE_HERO_ID_RE = re.compile(r"^\d{1,6}$")
_SAFE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff .'\-]{1,64}$")
_ALLOWED_REDIRECT_HOSTS = {"apexlol.info", "www.apexlol.info", "ddragon.leagueoflegends.com", "raw.communitydragon.org", "cdn.communitydragon.org"}
_lcu_warning_logged = False


def get_request_auth_token() -> str:
    return _request_auth_token


def _get_resource_path(relative_path: str) -> str:
    candidates = [
        os.path.join(RESOURCE_DIR, relative_path),
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
        # 冻结包里 `_MEIPASS/assets` 只是只读的捆绑副本；
        # 运行时补齐和缓存头像必须落到 exe 同级目录，保证 Web 端和桌面端看到同一份资源。
        if getattr(sys, "frozen", False):
            _assets_dir = os.path.join(BASE_DIR, "assets")
        else:
            _assets_dir = _get_resource_path("assets")
        os.makedirs(_assets_dir, exist_ok=True)
    return _assets_dir


def is_safe_png_asset_name(filename: str) -> bool:
    """校验海克斯图标文件名只包含安全字符，并固定为 png。"""
    raw_value = str(filename or "").strip()
    normalized = os.path.basename(raw_value)
    if not raw_value or normalized != raw_value:
        return False
    return bool(_SAFE_ASSET_RE.fullmatch(normalized))


def is_allowed_local_origin(origin: str | None) -> bool:
    """校验浏览器来源是否来自本机页面。

    只接受来自本机页面的浏览器请求；缺失 `Origin` 的请求一律拒绝。
    """
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in _SAFE_LOCAL_HOSTS


def get_active_web_port() -> int:
    return _active_web_port


def set_active_web_port(port: int) -> None:
    global _active_web_port
    _active_web_port = port


def write_active_web_port(port: int) -> None:
    fd, tmp_path = tempfile.mkstemp(prefix="web-port-", suffix=".tmp", dir=os.path.dirname(WEB_PORT_FILE))
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(port))
    os.replace(tmp_path, WEB_PORT_FILE)


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
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"https"}:
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in _ALLOWED_REDIRECT_HOSTS


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

    remote_url = resolve_apexlol_hextech_icon_url(augment_name, config_dir=CONFIG_DIR)
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
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
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

    _augment_cache_executor.submit(_worker)


def request_background_refresh(force: bool = False) -> bool:
    """按单飞模式触发后台刷新，避免接口并发下重复创建刷新线程。"""
    global _background_refresh_inflight
    with _background_refresh_lock:
        if _background_refresh_inflight:
            return False
        _background_refresh_inflight = True

    def _worker() -> None:
        global _background_refresh_inflight
        try:
            refresh_backend_data(force=force)
        except Exception:
            logger.exception("后台刷新线程执行失败。")
        finally:
            with _background_refresh_lock:
                _background_refresh_inflight = False

    threading.Thread(
        target=_worker,
        daemon=True,
        name="background-refresh-singleflight",
    ).start()
    return True


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


def is_safe_internal_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == "http" and str(parsed.hostname or "").strip() == "127.0.0.1"


def open_managed_browser(url: str, replace_existing: bool = False) -> bool:
    global _managed_browser_process

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
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

    try:
        webbrowser.open(url)
        logger.info("已通过系统默认浏览器打开：%s", url)
        return True
    except Exception as exc:
        logger.warning("打开浏览器失败：%s", exc)
        return False


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
    """读取最新战报 CSV，并复用内存缓存避免重复解析。"""
    try:
        return _csv_loader.get_df()
    except Exception as exc:
        logger.error("CSV 刷新失败：%s", exc)
        return pd.DataFrame()


def _get_runtime_df_signature() -> Tuple[str, float]:
    return (_csv_loader.cached_path, _csv_loader.cached_mtime)


def clear_preloaded_hextech_payloads() -> None:
    with _preloaded_hextech_lock:
        _preloaded_hextech_payloads.clear()
        _preloaded_hextech_pending.clear()
        global _preloaded_hextech_signature
        _preloaded_hextech_signature = ("", 0.0)


def get_preloaded_hextech_payload(hero_name: str) -> Optional[dict]:
    canonical_name = resolve_canonical_hero_name(hero_name)
    if not canonical_name:
        return None

    current_signature = _get_runtime_df_signature()
    with _preloaded_hextech_lock:
        global _preloaded_hextech_signature
        if _preloaded_hextech_signature != current_signature:
            _preloaded_hextech_payloads.clear()
            _preloaded_hextech_pending.clear()
            _preloaded_hextech_signature = current_signature
            return None

        payload = _preloaded_hextech_payloads.get(canonical_name)
        return payload if isinstance(payload, dict) else None


def queue_preload_hextech_payloads(hero_names: List[str]) -> bool:
    canonical_names = []
    for raw_name in hero_names:
        canonical_name = resolve_canonical_hero_name(raw_name)
        if canonical_name and canonical_name not in canonical_names:
            canonical_names.append(canonical_name)
    if not canonical_names:
        return False

    df = get_df()
    if df.empty:
        return False

    current_signature = _get_runtime_df_signature()
    with _preloaded_hextech_lock:
        global _preloaded_hextech_signature
        if _preloaded_hextech_signature != current_signature:
            _preloaded_hextech_payloads.clear()
            _preloaded_hextech_pending.clear()
            _preloaded_hextech_signature = current_signature

        queued_any = False
        for canonical_name in canonical_names:
            if canonical_name in _preloaded_hextech_payloads or canonical_name in _preloaded_hextech_pending:
                continue
            _preloaded_hextech_pending.add(canonical_name)
            queued_any = True

            def _worker(target_name: str = canonical_name, df_snapshot: pd.DataFrame = df.copy(), signature: Tuple[str, float] = current_signature) -> None:
                try:
                    payload = process_hextechs_data(df_snapshot, target_name, use_runtime_cache=True, log_columns=False)
                    if not isinstance(payload, dict) or not payload.get("comprehensive"):
                        return
                    with _preloaded_hextech_lock:
                        if _preloaded_hextech_signature != signature:
                            return
                        _preloaded_hextech_payloads[target_name] = payload
                except Exception:
                    logger.exception("海克斯预热失败：hero=%s", target_name)
                finally:
                    with _preloaded_hextech_lock:
                        _preloaded_hextech_pending.discard(target_name)

            _preloaded_hextech_executor.submit(_worker)

    return queued_any


async def get_df_with_refresh(timeout: float = 25.0) -> pd.DataFrame:
    """在 CSV 缺失时触发一次后台刷新，并在超时前轮询等待结果。"""
    df = get_df()
    if not df.empty:
        return df

    await asyncio.to_thread(refresh_backend_data, False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        df = get_df()
        if not df.empty:
            return df
        await asyncio.sleep(0.5)
    return df


@dataclass
class JSONFileCache:
    path: str = ""
    mtime: float = 0.0
    data: dict = field(default_factory=dict)


_synergy_cache = JSONFileCache()
_champion_snapshot_cache = JSONFileCache()
_live_hextech_cache = JSONFileCache()


def get_live_champion_snapshot_df(force_refresh: bool = False) -> pd.DataFrame:
    """读取远端轻量英雄快照，作为冷启动或本地 CSV 缺失时的回退数据源。"""
    cache_path = "live_champion_snapshot"
    now = time.time()
    if (
        not force_refresh
        and _champion_snapshot_cache.path == cache_path
        and _champion_snapshot_cache.data
        and (now - _champion_snapshot_cache.mtime) < 180
    ):
        payload = _champion_snapshot_cache.data
    else:
        try:
            session = get_advanced_session()
            response = None
            payload = None
            for url in HEXTECH_CHAMPION_STATS_URLS:
                try:
                    response = session.get(url, timeout=12, verify=True)
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, list):
                        break
                    payload = None
                except Exception:
                    payload = None
            if not isinstance(payload, list):
                return pd.DataFrame()
            _champion_snapshot_cache.path = cache_path
            _champion_snapshot_cache.mtime = now
            _champion_snapshot_cache.data = payload
        except Exception as exc:
            logger.warning("轻量英雄快照拉取失败：%s", exc)
            return pd.DataFrame()

    core_data = load_champion_core_data()
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        champ_id = str(item.get("championId", "")).strip()
        hero_info = core_data.get(champ_id, {})
        hero_name = hero_info.get("name", "")
        if not hero_name:
            continue
        try:
            rows.append(
                {
                    "英雄名称": hero_name,
                    "英雄胜率": float(item.get("winRate", 0) or 0),
                    "英雄出场率": float(item.get("pickRate", 0) or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def get_live_hextech_snapshot_df(hero_name: str, force_refresh: bool = False) -> pd.DataFrame:
    """按单英雄拉取海克斯快照，并缓存短周期结果用于详情页回退。"""
    hero_name = resolve_canonical_hero_name(hero_name)
    if not hero_name:
        return pd.DataFrame()

    cache_path = f"live_hextech_snapshot::{hero_name}"
    now = time.time()
    if (
        not force_refresh
        and _live_hextech_cache.path == cache_path
        and _live_hextech_cache.data
        and (now - _live_hextech_cache.mtime) < 180
    ):
        rows = _live_hextech_cache.data.get("rows", [])
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    core_data = load_champion_core_data()
    champ_id = None
    for key, value in core_data.items():
        if str(value.get("name", "")).strip() == hero_name:
            champ_id = str(key)
            break
    if not champ_id:
        return pd.DataFrame()

    truth_dict = load_augment_map()
    if not truth_dict:
        return pd.DataFrame()

    session = get_advanced_session()
    try:
        aug_response = None
        for url in HEXTECH_AUGMENT_METADATA_URLS:
            candidate = fetch_with_retry(session, url, timeout=6)
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), dict):
                    aug_response = candidate
                    break
            except Exception:
                continue
        stats_response = None
        for url in HEXTECH_CHAMPION_STATS_URLS:
            candidate = fetch_with_retry(session, url, timeout=6)
            if candidate is None:
                continue
            try:
                if isinstance(candidate.json(), list):
                    stats_response = candidate
                    break
            except Exception:
                continue
        if aug_response is None or stats_response is None:
            return pd.DataFrame()

        aug_data = aug_response.json()
        stats_list = stats_response.json()
        if not isinstance(aug_data, dict) or not isinstance(stats_list, list):
            return pd.DataFrame()

        aug_id_map = {}
        for raw_key, raw_item in aug_data.items():
            item = raw_item if isinstance(raw_item, dict) else {}
            aug_id = str(raw_key)
            aug_id_map[aug_id] = _clean_augment_text(item.get("displayName"))

        champ_stats = next(
            (item for item in stats_list if str(item.get("championId", "")).strip() == champ_id),
            None,
        )
        if not champ_stats:
            return pd.DataFrame()

        detail_response = None
        for url in build_hextech_detail_urls(champ_id):
            detail_response = fetch_with_retry(session, url, timeout=6)
            if detail_response is not None and detail_response.status_code == 200 and detail_response.text:
                break
        if detail_response is None or detail_response.status_code != 200 or not detail_response.text:
            return pd.DataFrame()

        rows = extract_champion_stats(
            detail_response.text,
            aug_id_map,
            truth_dict,
            champ_id,
            hero_name,
            champ_stats,
        )
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["胜率差"] = df["海克斯胜率"] - df["英雄胜率"]
        _live_hextech_cache.path = cache_path
        _live_hextech_cache.mtime = now
        _live_hextech_cache.data = {"rows": rows}
        logger.info("单英雄海克斯快照已就绪：hero=%s rows=%s", hero_name, len(rows))
        return df
    except Exception as exc:
        logger.warning("单英雄海克斯快照拉取失败：hero=%s error=%s", hero_name, exc)
        return pd.DataFrame()


def get_synergy_data() -> dict:
    json_path = os.path.join(CONFIG_DIR, "Champion_Synergy.json")
    try:
        current_mtime = os.path.getmtime(json_path)
    except OSError:
        return _synergy_cache.data

    if json_path != _synergy_cache.path or current_mtime != _synergy_cache.mtime:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _synergy_cache.path = json_path
            _synergy_cache.mtime = current_mtime
            _synergy_cache.data = data
            logger.info("Champion_Synergy.json 缓存已刷新")
        except Exception as exc:
            logger.error("协同数据文件加载失败：%s", exc)
            return _synergy_cache.data
    return _synergy_cache.data


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
        self.max_connections = 50

    async def connect(self, ws) -> None:
        async with self._lock:
            if len(self.active) >= self.max_connections:
                await ws.close(code=1013, reason="too_many_connections")
                return
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws) -> None:
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            snapshot = list(self.active)
        dead = []
        for ws in snapshot:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
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
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0
    state_version: int = 0
    updated_at: float = 0.0


_lcu_state = LCUState()
_lcu_state_lock = threading.RLock()


def get_live_state_payload() -> dict:
    with _lcu_state_lock:
        return {
            "champion_ids": sorted(_lcu_state.current_ids),
            "local_champion_id": _lcu_state.local_champ_id,
            "local_champion_name": _lcu_state.local_champ_name,
            "state_version": _lcu_state.state_version,
            "updated_at": _lcu_state.updated_at,
        }


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
                    await asyncio.sleep(2)
                    continue

            if not current_token or not current_port:
                await asyncio.sleep(2)
                continue
            auth = base64.b64encode(f"riot:{current_token}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
            url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

            _log_lcu_tls_warning_once()
            res = await asyncio.to_thread(_lcu_session.get, url, headers=headers, verify=False, timeout=3)

            if res.status_code == 200:
                data = res.json()
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count = 0

                available_ids = {str(c["championId"]) for c in data.get("benchChampions", [])}
                for player in data.get("myTeam", []):
                    if player.get("cellId") == data.get("localPlayerCellId") and player.get("championId") != 0:
                        available_ids.add(str(player["championId"]))

                with _lcu_state_lock:
                    ids_changed = available_ids != _lcu_state.current_ids
                    if ids_changed:
                        _lcu_state.current_ids = available_ids.copy()
                        _lcu_state.state_version += 1
                        _lcu_state.updated_at = time.time()
                if ids_changed:
                    preload_names = []
                    for champion_id in available_ids:
                        hero_name, _ = get_champion_info(str(champion_id))
                        if hero_name:
                            preload_names.append(hero_name)
                    if preload_names:
                        await asyncio.to_thread(queue_preload_hextech_payloads, preload_names)
                    await manager.broadcast(
                        {
                            "type": "champion_update",
                            "champion_ids": list(available_ids),
                            "timestamp": time.time(),
                        }
                    )

                local_cell_id = data.get("localPlayerCellId")
                local_champion_id = None
                for player in data.get("myTeam", []):
                    if player.get("cellId") == local_cell_id:
                        local_champion_id = player.get("championId")
                        break

                if local_champion_id and local_champion_id > 0:
                    with _lcu_state_lock:
                        prev_champ_id = _lcu_state.local_champ_id
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
                        if hero_name:
                            await asyncio.to_thread(request_preload_hextech_payload, hero_name)
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

                    elif prev_champ_id == local_champion_id and AUTO_JUMP_ENABLED and hero_name:
                        await asyncio.to_thread(request_preload_hextech_payload, hero_name)
            elif res.status_code == 404:
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count += 1
                    has_local_champ = _lcu_state.local_champ_id is not None
                    if has_local_champ:
                        _lcu_state.local_champ_id = None
                        _lcu_state.local_champ_name = None
                        _lcu_state.current_ids = set()
                    consecutive_404_count = _lcu_state.consecutive_404_count
                if has_local_champ:
                    clear_preloaded_hextech_payloads()
                if consecutive_404_count >= 5:
                    logger.warning("LCU 连续返回 404 五次，重置连接状态（count=%s）", consecutive_404_count)
                    with _lcu_state_lock:
                        _lcu_state.port = None
                        _lcu_state.token = None
                        _lcu_state.consecutive_404_count = 0
            elif res.status_code in (401, 403):
                logger.warning("LCU token 失效或未授权（401/403），重置连接状态。")
                clear_preloaded_hextech_payloads()
                with _lcu_state_lock:
                    _lcu_state.port = None
                    _lcu_state.token = None
            else:
                logger.warning("LCU 响应异常状态码=%s，重置连接状态。", res.status_code)
                clear_preloaded_hextech_payloads()
                with _lcu_state_lock:
                    _lcu_state.port = None
        except requests.exceptions.ConnectionError as exc:
            logger.warning("LCU 连接错误：%s", exc)
            clear_preloaded_hextech_payloads()
            with _lcu_state_lock:
                _lcu_state.port = None
                _lcu_state.token = None
        except Exception as exc:
            logger.warning("LCU 轮询失败：%s", exc)

        await asyncio.sleep(1.5)


async def csv_watcher_loop() -> None:
    """监视最新战报 CSV 的修改时间，并在文件切换后广播数据刷新事件。"""
    prev_mtime = 0.0
    while True:
        try:
            get_df()
            current_mtime = _csv_loader.cached_mtime
            if current_mtime > prev_mtime and prev_mtime != 0.0:
                logger.info("CSV 已更新：%s", os.path.basename(_csv_loader.cached_path))
                await manager.broadcast({"type": "data_updated"})
            prev_mtime = current_mtime
        except (OSError, IOError) as exc:
            logger.warning("CSV 监视器错误：%s", exc)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_app):
    """Web 生命周期钩子。

    启动时拉起一次后台自愈/刷新，并创建 LCU 与 CSV 两条长生命周期任务；
    退出时统一取消这些后台任务，避免悬挂协程。
    """
    scraper_thread = threading.Thread(
        target=refresh_backend_data,
        kwargs={"force": False},
        daemon=True,
        name="backend-refresh-startup",
    )
    scraper_thread.start()
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
