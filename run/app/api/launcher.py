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
import webbrowser
from urllib.parse import quote, unquote
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

import pandas as pd
import psutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.core.champion_aliases import load_champion_alias_index
from icon_resolver import (
    ensure_augment_icon_cached,
    find_existing_augment_asset_filename,
    resolve_apexlol_hextech_icon_url,
)
from services.runtime_precomputed_cache import (
    load_precomputed_champion_list,
    load_precomputed_hextech_for_hero,
)
from services.scrape_augments import (
    find_augment_catalog_entry,
    load_augment_icon_manifest,
)
from app.core.data_processor import process_champions_data, process_hextechs_data
from services.scrape_hextech import _clean_augment_text, extract_champion_stats, fetch_with_retry
from services.sync_hero_data import (
    BASE_DIR,
    CONFIG_DIR,
    RESOURCE_DIR,
    get_advanced_session,
    load_augment_map,
    load_champion_core_data,
)
from backend_refresh import get_startup_status, refresh_backend_data
from log_utils import ensure_utf8_stdio
from app.core.runtime_data import CachedDataFrameLoader, get_latest_csv

ensure_utf8_stdio()

# 模块日志。
logger = logging.getLogger(__name__)

# 网页服务默认端口，可通过环境变量覆盖。
SERVER_PORT = int(os.getenv("HEXTECH_PORT", "8000"))
WEB_PORT_FILE = os.path.join(CONFIG_DIR, "web_server_port.txt")
ACTIVE_WEB_PORT = SERVER_PORT
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")
BROWSER_PROFILE_DIR = os.path.join(CONFIG_DIR, "browser_profile")

_managed_browser_process: Optional[subprocess.Popen] = None
_managed_browser_lock = threading.Lock()
_augment_cache_pending: Set[str] = set()
_augment_cache_pending_lock = threading.Lock()
_csv_loader = CachedDataFrameLoader(get_latest_csv)


def _write_active_web_port(port: int) -> None:
    tmp_path = WEB_PORT_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(str(port))
    os.replace(tmp_path, WEB_PORT_FILE)


def _resolve_remote_augment_icon_url(catalog_entry: Optional[dict], fallback_name: str) -> str:
    if catalog_entry:
        manifest_filename = str(catalog_entry.get("filename", "")).strip().lower()
        if manifest_filename:
            remote_filename = manifest_filename
            if remote_filename.count(".") > 1:
                remote_filename = remote_filename.split(".", 1)[0] + ".png"
            return f"https://cdn.dtodo.cn/hextech/augment-icons/{quote(remote_filename, safe='')}"
        manifest_url = str(catalog_entry.get("icon_url", "")).strip()
        if manifest_url and not manifest_url.startswith("/assets/"):
            return manifest_url
        augment_name = str(catalog_entry.get("name", "")).strip() or fallback_name
    else:
        augment_name = fallback_name

    remote_url = resolve_apexlol_hextech_icon_url(augment_name, config_dir=CONFIG_DIR)
    if remote_url and not remote_url.startswith("/assets/"):
        return remote_url
    return ""


def _download_augment_icon_from_remote(augment_name: str, icon_filename: str) -> Optional[str]:
    remote_url = _resolve_remote_augment_icon_url({"name": augment_name, "filename": icon_filename}, augment_name)
    if not remote_url:
        return None

    os.makedirs(_assets_dir, exist_ok=True)
    target_path = os.path.join(_assets_dir, icon_filename)
    tmp_path = target_path + ".tmp"
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


def _queue_augment_icon_cache(icon_filename: str, augment_name: str = "") -> None:
    normalized = str(icon_filename or "").strip()
    if not normalized:
        return

    with _augment_cache_pending_lock:
        if normalized in _augment_cache_pending:
            return
        _augment_cache_pending.add(normalized)

    def _worker() -> None:
        try:
            cached_path = ensure_augment_icon_cached(normalized, asset_dir=_assets_dir)
            if cached_path and os.path.exists(cached_path):
                return
            if augment_name:
                _download_augment_icon_from_remote(augment_name, normalized)
        finally:
            with _augment_cache_pending_lock:
                _augment_cache_pending.discard(normalized)

    threading.Thread(target=_worker, daemon=True).start()


def _iter_browser_candidates() -> List[str]:
    configured = os.getenv("HEXTECH_BROWSER")
    candidates = []
    if configured:
        candidates.append(configured)
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


def _open_managed_browser(url: str, replace_existing: bool = False) -> bool:
    global _managed_browser_process

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

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
            except OSError as e:
                logger.debug("启动浏览器 %s 失败：%s", browser_path, e)

    try:
        webbrowser.open(url)
        logger.info("已通过系统默认浏览器打开：%s", url)
        return True
    except Exception as e:
        logger.warning("打开浏览器失败：%s", e)
        return False


def _build_detail_url(hero_id: str, hero_name: str, en_name: str) -> str:
    return (
        f"http://127.0.0.1:{ACTIVE_WEB_PORT}/detail.html"
        f"?hero={quote(hero_name or '', safe='')}"
        f"&id={quote(str(hero_id), safe='')}"
        f"&en={quote(en_name or '', safe='')}"
        f"&auto=1"
    )

_champion_core_cache: Optional[dict] = None


def _ensure_champion_cache() -> dict:
    # 懒加载英雄核心数据，减少重复读取。
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logger.warning("英雄核心数据加载失败：%s", e)
            _champion_core_cache = {}
    return _champion_core_cache


def get_champion_name(champ_id: str) -> str:
    # 根据英雄 ID 读取中文名。
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        return cache[champ_id_str].get('name', '')
    return ''


def get_champion_info(champ_id: str) -> Tuple[str, str]:
    # 返回中文名和英文名。
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        data = cache[champ_id_str]
        return data.get('name', ''), data.get('en_name', '')
    return '', ''


def _resolve_core_hero_record(query: str) -> Optional[dict]:
    normalized_query = str(query or "").strip().lower()
    if not normalized_query:
        return None

    cache = _ensure_champion_cache()
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


def _resolve_canonical_hero_name(query: str) -> str:
    record = _resolve_core_hero_record(query)
    if record:
        return str(record.get("heroName", "")).strip()
    return str(query or "").strip()

def _resolve_champion_id(query: str) -> str:
    raw_query = str(query or "").strip()
    if not raw_query:
        return ""
    if raw_query.isdigit():
        return raw_query

    record = _resolve_core_hero_record(raw_query)
    if record:
        hero_id = str(record.get("heroId", "")).strip()
        if hero_id:
            return hero_id

    return ""


def _get_ddragon_version() -> str:
    # 读取本地版本号，失败时使用默认值。
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("无法读取 hero_version.txt，改用内置版本。")
    return "14.3.1"


# 请求体：前端点击英雄后发送到跳转接口。

class RedirectRequest(BaseModel):
    hero_id: str
    hero_name: str


# 打包环境兼容的资源路径解析。

def get_resource_path(relative_path: str) -> str:
    # 返回打包环境兼容的资源路径。
    candidates = [
        os.path.join(RESOURCE_DIR, relative_path),
        os.path.join(BASE_DIR, relative_path),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def _html_file_response(filename: str) -> FileResponse:
    # 返回网页文件并显式声明中文编码，避免浏览器乱码。
    return FileResponse(
        os.path.join(_static_dir, filename),
        media_type="text/html; charset=utf-8",
    )


def get_df() -> pd.DataFrame:
    # 返回最新的英雄数据表。
    try:
        return _csv_loader.get_df()
    except Exception as e:
        logger.error("CSV 刷新失败：%s", e)
        return pd.DataFrame()


async def _get_df_with_refresh(timeout: float = 25.0) -> pd.DataFrame:
    # 打包版首次启动可能没有本地 CSV，这里主动触发一次刷新并等待结果。
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


def _get_live_champion_snapshot_df(force_refresh: bool = False) -> pd.DataFrame:
    # 当完整 CSV 尚未生成时，使用轻量英雄统计快照先撑起首页。
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
            response = session.get(
                "https://hextech.dtodo.cn/data/champions-stats.json",
                timeout=12,
                verify=True,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                return pd.DataFrame()
            _champion_snapshot_cache.path = cache_path
            _champion_snapshot_cache.mtime = now
            _champion_snapshot_cache.data = payload
        except Exception as e:
            logger.warning("轻量英雄快照拉取失败：%s", e)
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
            rows.append({
                "英雄名称": hero_name,
                "英雄胜率": float(item.get("winRate", 0) or 0),
                "英雄出场率": float(item.get("pickRate", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _get_live_hextech_snapshot_df(hero_name: str, force_refresh: bool = False) -> pd.DataFrame:
    # 完整 CSV 尚未就绪时，直接抓当前英雄的远端数据，优先让左侧海克斯排序可用。
    hero_name = _resolve_canonical_hero_name(hero_name)
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
        aug_response = fetch_with_retry(
            session,
            "https://hextech.dtodo.cn/data/aram-mayhem-augments.zh_cn.json",
            timeout=6,
        )
        stats_response = fetch_with_retry(
            session,
            "https://hextech.dtodo.cn/data/champions-stats.json",
            timeout=6,
        )
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

        detail_response = fetch_with_retry(
            session,
            f"https://hextech.dtodo.cn/zh-CN/champion-stats/{champ_id}",
            timeout=6,
        )
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
    except Exception as e:
        logger.warning("单英雄海克斯快照拉取失败：hero=%s error=%s", hero_name, e)
        return pd.DataFrame()


# 结构化数据缓存。

@dataclass
class JSONFileCache:
    # 记录数据文件的路径、修改时间和解析结果。
    path: str = ""
    mtime: float = 0.0
    data: dict = field(default_factory=dict)

_synergy_cache = JSONFileCache()
_champion_snapshot_cache = JSONFileCache()
_live_hextech_cache = JSONFileCache()


def _get_synergy_data() -> dict:
    # 读取并缓存协同数据文件。
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
        except Exception as e:
            logger.error("协同数据文件加载失败：%s", e)
            return _synergy_cache.data
    return _synergy_cache.data


# 网页套接字连接管理。

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict):
    # 先拷贝连接列表，再广播，避免遍历时被并发修改。
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


# 本地客户端轮询状态与连接管理。

# 是否在检测到本地锁定英雄时自动跳转详情页。
AUTO_JUMP_ENABLED = True


@dataclass
class LCUState:
    # 保存当前连接、会话和英雄选择状态。
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0

_lcu_state = LCUState()


def _create_lcu_session() -> requests.Session:
    # 复用带重试的会话，降低临时失败带来的抖动。
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

# 本地客户端请求复用会话。
_lcu_session = _create_lcu_session()


def _scan_lcu_process() -> tuple:
    # 扫描本地客户端进程并提取端口和令牌。
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


def _urllib3_disable_warnings():
    # 忽略本地客户端自签名证书告警。
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


async def lcu_polling_loop():
    # 持续轮询本地客户端会话。
    #
    # - 读取当前可选英雄列表。
    # - 找到本地玩家的英雄编号。
    # - 向前端广播选角变化。
    # - 连续异常时自动重置连接状态。
    #
    # 轮询失败时会继续重试，不会让服务退出。
    #
    #
    _urllib3_disable_warnings()
    while True:
        try:
            if not _lcu_state.port:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    _lcu_state.port = port
                    _lcu_state.token = token
                    logger.info("已检测到 LCU 连接，端口=%s", port)
                else:
                    await asyncio.sleep(2)
                    continue

            auth = base64.b64encode(
                f"riot:{_lcu_state.token}".encode()
            ).decode()
            headers = {
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
            }
            url = f"https://127.0.0.1:{_lcu_state.port}/lol-champ-select/v1/session"

            res = await asyncio.to_thread(
                _lcu_session.get, url, headers=headers, verify=False, timeout=3
            )

            if res.status_code == 200:
                data = res.json()
                # 成功响应后重置 404 计数。
                _lcu_state.consecutive_404_count = 0

                # 收集当前可选英雄编号。
                available_ids = {
                    str(c["championId"])
                    for c in data.get("benchChampions", [])
                }
                for p in data.get("myTeam", []):
                    if (
                        p.get("cellId") == data.get("localPlayerCellId")
                        and p.get("championId") != 0
                    ):
                        available_ids.add(str(p["championId"]))

                if available_ids != _lcu_state.current_ids:
                    _lcu_state.current_ids = available_ids.copy()
                    await manager.broadcast({
                        "type": "champion_update",
                        "champion_ids": list(available_ids),
                        "timestamp": time.time(),
                    })

                # 找到本地玩家所在的格子。
                local_cell_id = data.get("localPlayerCellId")
                local_champion_id = None

                # 在队伍列表中按格子编号找到本地玩家。
                for p in data.get("myTeam", []):
                    if p.get("cellId") == local_cell_id:
                        local_champion_id = p.get("championId")
                        break

                # 英雄编号大于 0 才表示已经锁定英雄。
                if local_champion_id and local_champion_id > 0:
                    prev_champ_id = _lcu_state.local_champ_id

                    if prev_champ_id != local_champion_id:
                        _lcu_state.local_champ_id = local_champion_id

                        # 读取英雄中英文名，供广播和日志使用。
                        hero_name, en_name = get_champion_info(str(local_champion_id))
                        _lcu_state.local_champ_name = hero_name

                        logger.info("LCU 已锁定英雄：%s (ID=%s)", hero_name, local_champion_id)

                        # 首次锁定时通知前端页面。
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast({
                                "type": "local_player_locked",
                                "champion_id": local_champion_id,
                                "hero_name": hero_name,
                                "en_name": en_name,
                            })
                        else:
                            logger.debug("自动跳转已关闭，跳过本地锁定广播。")

            elif res.status_code == 404:
                # 返回 404，说明会话暂时不存在或已切换。
                _lcu_state.consecutive_404_count += 1

                # 清空本地英雄状态，等待下一次会话恢复。
                if _lcu_state.local_champ_id is not None:
                    _lcu_state.local_champ_id = None
                    _lcu_state.local_champ_name = None
                    _lcu_state.current_ids = set()

                # 连续 5 次 404 后，主动重置连接信息。
                if _lcu_state.consecutive_404_count >= 5:
                    logger.warning("LCU 连续返回 404 五次，重置连接状态（count=%s）", _lcu_state.consecutive_404_count)
                    _lcu_state.port = None
                    _lcu_state.token = None
                    _lcu_state.consecutive_404_count = 0

            elif res.status_code in (401, 403):
                # 令牌失效或未授权，重新扫描进程并获取新会话。
                logger.warning("LCU token 失效或未授权（401/403），重置连接状态。")
                _lcu_state.port = None
                _lcu_state.token = None
            else:
                logger.warning("LCU 响应异常状态码=%s，重置连接状态。", res.status_code)
                _lcu_state.port = None

        except requests.exceptions.ConnectionError as e:
            logger.warning("LCU 连接错误：%s", e)
            _lcu_state.port = None
            _lcu_state.token = None
        except Exception as e:
            logger.warning("LCU 轮询失败：%s", e)

        await asyncio.sleep(1.5)


# 数据表变更轮询。

async def csv_watcher_loop():
    # 每 3 秒检查一次数据表是否更新。
    #
    # 如果文件发生变化，则向前端广播 `data_updated`。
    #

    prev_mtime = 0.0
    while True:
        try:
            # 触发数据刷新，更新缓存时间。
            get_df()
            current_mtime = _csv_loader.cached_mtime
            if current_mtime > prev_mtime and prev_mtime != 0.0:
                logger.info("CSV 已更新：%s", os.path.basename(_csv_loader.cached_path))
                await manager.broadcast({'type': 'data_updated'})
            prev_mtime = current_mtime
        except (OSError, IOError) as e:
            logger.warning("CSV 监视器错误：%s", e)
        await asyncio.sleep(3)


# 网页服务生命周期管理。

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行后台刷新和轮询任务。
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

app = FastAPI(lifespan=lifespan)

# 挂载静态资源目录，提供前端文件。
_static_dir = get_resource_path("static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# 运行时下载的图片和兜底资源都放在资源目录。
_assets_dir = get_resource_path("assets")
os.makedirs(_assets_dir, exist_ok=True)
# 这里的资源既包含英雄头像，也包含增强器图标缓存。


# 页面与接口路由。

@app.get("/")
async def read_index():
    # 返回首页文件。
    return _html_file_response("index.html")

@app.get("/index.html")
async def read_index_explicit():
    # 显式访问首页路径时也返回首页。
    return _html_file_response("index.html")

@app.get("/detail.html")
async def read_detail():
    # 返回详情页文件。
    return _html_file_response("detail.html")

@app.get("/favicon.ico")
async def favicon():
    # 返回空的站点图标响应，避免 404 噪音。
    return Response(status_code=204)

@app.get("/assets/{filename}")
async def get_asset(filename: str):
    # 按文件名返回资源；海克斯图标优先走本地缓存，不命中时立即回退远端并后台补缓存。
    local_path = os.path.join(_assets_dir, filename)
    real_requested = os.path.normcase(os.path.realpath(local_path))
    real_assets_dir = os.path.normcase(os.path.realpath(_assets_dir))
    if not real_requested.startswith(real_assets_dir + os.sep) and real_requested != real_assets_dir:
        logger.warning("已阻止目录遍历：%s -> %s", filename, real_requested)
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)
    if os.path.exists(local_path):
        return FileResponse(local_path)
    if filename.endswith('.png') and not filename[:-4].isdigit():
        try:
            file_stem = unquote(filename[:-4])
            catalog_entry = find_augment_catalog_entry(file_stem, CONFIG_DIR)
            if catalog_entry:
                augment_name = str(catalog_entry.get("name", "")).strip() or file_stem
                mapped_filename = str(catalog_entry.get("filename", "")).strip()
                if mapped_filename:
                    local_mapped = find_existing_augment_asset_filename(_assets_dir, mapped_filename)
                    if local_mapped:
                        return FileResponse(os.path.join(_assets_dir, local_mapped))
                    _queue_augment_icon_cache(mapped_filename, augment_name)

                remote_icon_url = _resolve_remote_augment_icon_url(catalog_entry, augment_name)
                if remote_icon_url:
                    return RedirectResponse(url=remote_icon_url, status_code=307)

            local_fallback = find_existing_augment_asset_filename(_assets_dir, filename)
            if local_fallback:
                return FileResponse(os.path.join(_assets_dir, local_fallback))
            if re.fullmatch(r"[A-Za-z0-9._-]+", file_stem):
                _queue_augment_icon_cache(filename, file_stem)
            remote_icon_url = _resolve_remote_augment_icon_url(catalog_entry, file_stem)
            if remote_icon_url:
                return RedirectResponse(url=remote_icon_url, status_code=307)
        except Exception as e:
            logger.debug("远程资源缓存失败：%s", e)

    # 普通英雄头像尝试从官方资源源回退。
    if filename.endswith('.png'):
        file_stem = filename[:-4]  # 这里的文件名形如“123.png”。
        hero_name = get_champion_name(file_stem)
        if hero_name:
            _, en_name = get_champion_info(file_stem)
            if en_name:
                version = _get_ddragon_version()
                ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png"
                return RedirectResponse(url=ddragon_url, status_code=307)
        logger.debug("资源本地不存在，DDragon 回退也失败：%s", filename)

    return JSONResponse(content={"error": "资源未找到"}, status_code=404)

@app.get("/api/champions")
async def api_champions():
    df = get_df()
    if not df.empty:
        return JSONResponse(content=process_champions_data(df))

    # 完整 CSV 不存在时，首页先使用轻量快照，避免长时间空白。
    threading.Thread(
        target=refresh_backend_data,
        kwargs={"force": False},
        daemon=True,
        name="api-champions-refresh",
    ).start()

    snapshot_df = await asyncio.to_thread(_get_live_champion_snapshot_df)
    if not snapshot_df.empty:
        return JSONResponse(content=process_champions_data(snapshot_df))

    df = await _get_df_with_refresh()
    return JSONResponse(content=process_champions_data(df))


@app.get("/api/startup_status")
async def api_startup_status():
    return JSONResponse(content=get_startup_status())


@app.get("/api/live_state")
async def api_live_state():
    return JSONResponse(content={
        "champion_ids": sorted(_lcu_state.current_ids),
        "local_champion_id": _lcu_state.local_champ_id,
        "local_champion_name": _lcu_state.local_champ_name,
    })


@app.get("/api/champion_aliases")
async def api_champion_aliases():
    # 返回首页搜索专用的静态英雄别名索引。
    try:
        payload = load_champion_alias_index()
        payload.sort(key=lambda item: item.get("heroName", ""))
        return JSONResponse(content=payload)
    except Exception as e:
        logger.warning("英雄别名索引读取失败：%s", e)
        return JSONResponse(content=[])

@app.get("/api/champion/{name}/hextechs")
async def api_champion_hextechs(name: str):
    canonical_name = _resolve_canonical_hero_name(name)
    df = await _get_df_with_refresh()
    if df.empty:
        live_df = await asyncio.to_thread(_get_live_hextech_snapshot_df, canonical_name)
        if not live_df.empty:
            return JSONResponse(content=process_hextechs_data(live_df, canonical_name))
    return JSONResponse(content=process_hextechs_data(df, canonical_name))

@app.get("/api/augment_icon_map")
async def api_augment_icon_map():
    # 返回海克斯图标映射，供前端或调试页查找。
    try:
        manifest = load_augment_icon_manifest(CONFIG_DIR)
        data = {}
        for item in manifest:
            name = str(item.get("name", "")).strip()
            filename = str(item.get("filename", "")).strip()
            remote_icon_url = str(item.get("icon_url", "")).strip()
            if not name:
                continue
            if filename:
                data[name] = f"/assets/{quote(filename, safe='')}"
            elif remote_icon_url:
                data[name] = remote_icon_url
        return JSONResponse(content=data)
    except Exception as e:
        logger.warning("统一海克斯目录图标映射读取失败：%s", e)
        return JSONResponse(content={})

@app.get("/api/synergies/{champ_id}")
async def api_synergies(champ_id: str):
    # 返回英雄协同数据。
    try:
        data = _get_synergy_data()
        if not data:
            return JSONResponse(content={"synergies": []})

        resolved_champ_id = _resolve_champion_id(champ_id)
        canonical_name = _resolve_canonical_hero_name(champ_id).lower()

        # 只按主编号、主名、称号、英文名归一后的结果匹配，不接入首页搜索别名索引。
        synergy_data = data.get(resolved_champ_id or champ_id, {})
        if not synergy_data:
            for key, value in data.items():
                if (
                    str(champ_id).lower() == key.lower()
                    or str(resolved_champ_id).lower() == key.lower()
                    or (canonical_name and canonical_name == key.lower())
                ):
                    synergy_data = value
                    break

        synergies = synergy_data.get("synergies", []) if synergy_data else []
        return JSONResponse(content={"synergies": synergies})
    except Exception as e:
        logger.warning("协同数据查询失败：%s", e)
        return JSONResponse(content={"synergies": []})

@app.post("/api/redirect")
async def api_redirect(req: RedirectRequest):
    # 处理前端点击后的重定向。

    # 先尝试从英雄编号还原中英文名。
    try:
        hero_name, en_name = get_champion_info(req.hero_id)
    except (ValueError, TypeError):
        # 编号不是合法文本时，回退为空字符串。
        hero_name, en_name = '', ''

    # 如果中文名缺失，退回前端传来的名称。
    if not hero_name:
        hero_name = req.hero_name

    # 当前没有前端连接时，直接由服务端打开详情页。
    if len(manager.active) == 0:
        url = _build_detail_url(req.hero_id, hero_name or req.hero_name, en_name)
        if _open_managed_browser(url, replace_existing=True):
            return JSONResponse(content={"status": "opened_browser"})
        return JSONResponse(content={"status": "浏览器打开失败"}, status_code=500)
    else:
        # 有前端在线时，直接广播给页面处理。
        await manager.broadcast({
            "type": "local_player_locked",
            "champion_id": req.hero_id,
            "hero_name": req.hero_name,
            "en_name": en_name
        })
        return JSONResponse(content={"status": "broadcast_sent"})

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)


# 端口探测与浏览器启动。

def find_available_port(start_port=8000, max_attempts=50):
    # 从起始端口开始查找可用端口。
    import socket

    for port_offset in range(max_attempts):
        port = start_port + port_offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"未能在端口范围 {start_port}-{start_port + max_attempts - 1} 找到可用端口")

def _open_chrome(port: int):
    # 打开浏览器访问当前网页服务。
    url = f"http://127.0.0.1:{port}"
    _open_managed_browser(url, replace_existing=True)


def run_web_server() -> None:
    global ACTIVE_WEB_PORT

    # 启动时先找可用端口，避免端口占用导致服务直接失败。
    actual_port = find_available_port(SERVER_PORT)
    if actual_port != SERVER_PORT:
        logger.info("端口 %s 已被占用，改用端口 %s", SERVER_PORT, actual_port)

    # 将实际端口写回配置，供界面程序动态读取。
    ACTIVE_WEB_PORT = actual_port
    _write_active_web_port(ACTIVE_WEB_PORT)
    if os.getenv("HEXTECH_OPEN_BROWSER", "1").lower() not in {"0", "false", "no"}:
        _open_chrome(actual_port)
    uvicorn.run(
        "app.api.launcher:app",
        host="127.0.0.1",
        port=actual_port,
        reload=False,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    run_web_server()


def run_web() -> None:
    run_web_server()
