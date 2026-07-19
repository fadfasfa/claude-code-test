# ruff: noqa: E402, F401
"""Web 服务运行时支撑层。

文件职责：
- 承载 Web 服务的长生命周期状态和请求热路径辅助逻辑
- 统一管理 LCU 轮询、CSV 监视、冷启动快照和资源缓存回退

核心输入：
- 已验证 Catalog、只读 `resources/assets` seed 与 `var/cache/assets` 运行态缓存
- LCU 本地接口、远端快照接口和海克斯图标资源

核心输出：
- Web API 可直接消费的运行时数据、缓存结果和广播事件

主要依赖：
- `hextech.modules.data.catalog.runtime_store`
- `hextech.modules.data.generation`
- `hextech.infrastructure.sources.version_sync`
- `hextech.modules.acquisition.common.icons`

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
import sys
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

import psutil
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from hextech.modules.game_context.client import ClientContextProvider
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.catalog.runtime_store import (
    ensure_private_runtime_dir,
    ensure_runtime_profile_dir,
    build_runtime_profile_path,
    build_runtime_state_path,
    write_private_runtime_state_file,
)
from hextech.modules.data.catalog.augment_lookup import find_augment_catalog_entry as find_augment_catalog_entry
from hextech.modules.acquisition.common.icons import (
    ensure_augment_icon_cached,
    find_existing_augment_asset_filename as find_existing_augment_asset_filename,
    is_safe_augment_icon_filename,
    is_safe_remote_icon_url,
    resolve_apexlol_hextech_icon_url,
)
from hextech.modules.vision.image_validation import is_valid_png_bytes, read_limited_response_bytes
from hextech.modules.data.catalog.version_catalog import load_champion_core_data
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.modules.data.ports.paths import (
    ASSET_DIR,
    BASE_DIR,
    BUNDLE_ROOT_DIR,
    var_path,
)


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_ensure_utf8_stdio()

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
_startup_status_file = build_runtime_state_path("startup_status.json")
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
    return _static_dir


def get_assets_dir() -> str:
    """返回唯一可写图片缓存；只在实际需要缓存时创建。"""

    global _assets_dir
    if _assets_dir is None:
        _assets_dir = os.fspath(var_path("cache", "assets"))
        os.makedirs(_assets_dir, exist_ok=True)
    return _assets_dir


def get_asset_search_dirs() -> tuple[str, ...]:
    """按运行缓存、只读 seed 的顺序返回图片查找根。"""

    roots = (get_assets_dir(), ASSET_DIR)
    return tuple(dict.fromkeys(os.path.abspath(root) for root in roots))


def get_catalog_dir() -> str:
    """返回已验证 runtime Catalog；不存在时由 loader 回退只读 seed。"""

    return os.fspath(load_active_catalog().root)


def find_local_asset_path(relative_path: str) -> Optional[str]:
    """在受控根目录中查找本地图片，缓存优先且不创建 seed 路径。"""

    for root in get_asset_search_dirs():
        candidate = safe_join_under_dir(root, relative_path)
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


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

    remote_url = resolve_apexlol_hextech_icon_url(augment_name, config_dir=get_catalog_dir())
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

    target_path = safe_join_under_dir(get_assets_dir(), f"augments/{safe_filename}")
    if not target_path:
        logger.warning("已阻止图标缓存目录穿越：%s", safe_filename)
        return None
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
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
            _champion_core_cache = load_champion_core_data(get_catalog_dir())
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
        with open(os.path.join(get_catalog_dir(), "hero_version.txt"), "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("无法读取 hero_version.txt，跳过 DDragon 回退。")
    return ""


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


__all__ = [name for name in globals() if not name.startswith("__")]
from hextech.interfaces.web.backend.lcu_runtime import *  # noqa: F403, F401
from hextech.interfaces.web.backend.app_lifecycle import *  # noqa: F403, F401
