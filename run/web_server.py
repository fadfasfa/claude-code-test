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
from html import unescape
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from data_processor import process_champions_data, process_hextechs_data
from hextech_query import get_latest_csv
from hero_sync import load_champion_core_data, CONFIG_DIR
from backend_refresh import refresh_backend_data

# 记录本模块的运行日志。
logger = logging.getLogger(__name__)

# Web 服务默认监听 8000，必要时可通过 HEXTECH_PORT 覆盖。
SERVER_PORT = int(os.getenv("HEXTECH_PORT", "8000"))
WEB_PORT_FILE = os.path.join(CONFIG_DIR, "web_server_port.txt")
ACTIVE_WEB_PORT = SERVER_PORT
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")
AUGMENT_ICON_SOURCE_FILE = os.path.join(CONFIG_DIR, "augment_icon_source.txt")
BROWSER_PROFILE_DIR = os.path.join(CONFIG_DIR, "browser_profile")
# 增强器图标来源标记，用于判断是否需要重新预取资源。
AUGMENT_ICON_SOURCE_ID = "apexlol"
APEXLOL_HEXTECH_INDEX_URL = "https://apexlol.info/zh/hextech/"
APEXLOL_HEXTECH_IMAGE_URL = "https://apexlol.info/images/hextech/{slug}.webp"
APEXLOL_HEXTECH_MAP_FILE = os.path.join(CONFIG_DIR, "Augment_Apexlol_Map.json")

_managed_browser_process: Optional[subprocess.Popen] = None
_managed_browser_lock = threading.Lock()


def _write_active_web_port(port: int) -> None:
    tmp_path = WEB_PORT_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(str(port))
    os.replace(tmp_path, WEB_PORT_FILE)


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

# 懒加载英雄核心数据缓存。

_champion_core_cache: Optional[dict] = None


def _ensure_champion_cache() -> dict:
    # 从共享缓存里读取英雄核心数据，减少重复读盘。
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
    # 同时返回中文名和英文名，供页面和 CDN 回退逻辑共用。
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        data = cache[champ_id_str]
        return data.get('name', ''), data.get('en_name', '')
    return '', ''


def _get_ddragon_version() -> str:
    # 从本地版本文件读取 DDragon 版本，失败时回退默认值。
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("无法读取 hero_version.txt，改用内置版本。")
    return "14.3.1"


_augment_icon_map_cache: Tuple[float, dict] = (0.0, {})
_augment_prefetch_lock = threading.Lock()
_augment_prefetch_mtime = 0.0


def _normalize_augment_name(name: str) -> str:
    name = str(name).lower()
    for token in (" ", "-", "_", "(", ")", "[", "]", "'", '"', "."):
        name = name.replace(token, "")
    return name


def _normalize_augment_filename(value: str) -> str:
    return os.path.basename(str(value).strip()).lower()


def _load_augment_icon_map() -> dict:
    global _augment_icon_map_cache

    icon_map_path = os.path.join(CONFIG_DIR, "Augment_Icon_Map.json")
    try:
        current_mtime = os.path.getmtime(icon_map_path)
    except OSError:
        return _augment_icon_map_cache[1]

    cached_mtime, cached_data = _augment_icon_map_cache
    if cached_mtime == current_mtime and cached_data:
        return cached_data

    try:
        with open(icon_map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _augment_icon_map_cache = (current_mtime, data)
            return data
    except Exception as e:
        logger.warning("增强器图标映射加载失败：%s", e)

    return cached_data


_apexlol_hextech_map_cache: Tuple[float, dict] = (0.0, {})
_apexlol_hextech_map_lock = threading.Lock()


def _strip_html_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_apexlol_hextech_slug(value: str) -> str:
    value = str(value).strip()
    return value.lstrip("/").split("?")[0].split("#")[0]


def _load_apexlol_hextech_map(force_refresh: bool = False) -> dict:
    global _apexlol_hextech_map_cache

    with _apexlol_hextech_map_lock:
        cached_mtime, cached_data = _apexlol_hextech_map_cache
        if not force_refresh and cached_data:
            return cached_data

    if not force_refresh and os.path.exists(APEXLOL_HEXTECH_MAP_FILE):
        try:
            current_mtime = os.path.getmtime(APEXLOL_HEXTECH_MAP_FILE)
            cached_mtime, cached_data = _apexlol_hextech_map_cache
            if cached_mtime == current_mtime and cached_data:
                return cached_data
            with open(APEXLOL_HEXTECH_MAP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                _apexlol_hextech_map_cache = (current_mtime, data)
                return data
        except Exception as e:
            logger.debug("读取 apexlol 海克斯图标映射失败：%s", e)

    try:
        response = requests.get(
            APEXLOL_HEXTECH_INDEX_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        html = response.text
    except Exception as e:
        logger.warning("获取 apexlol 海克斯图标映射失败：%s", e)
        return cached_data

    name_to_slug: dict = {}
    for match in re.finditer(r'href="/zh/hextech/([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        slug = _normalize_apexlol_hextech_slug(match.group(1))
        inner_html = match.group(2)
        title = _strip_html_tags(inner_html)
        if not slug or not title:
            continue
        # 优先保留首次出现的准确名称。
        name_to_slug.setdefault(title, slug)
        normalized_title = _normalize_augment_name(title)
        name_to_slug.setdefault(normalized_title, slug)

    if name_to_slug:
        try:
            tmp_path = APEXLOL_HEXTECH_MAP_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(name_to_slug, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, APEXLOL_HEXTECH_MAP_FILE)
            _apexlol_hextech_map_cache = (time.time(), name_to_slug)
        except Exception as e:
            logger.debug("写入 apexlol 海克斯图标映射失败：%s", e)
        return name_to_slug

    return cached_data


def _resolve_apexlol_hextech_icon_url(hextech_name: str) -> str:
    slug_map = _load_apexlol_hextech_map()
    candidates = [
        str(hextech_name).strip(),
        _normalize_augment_name(hextech_name),
    ]
    slug = ""
    for candidate in candidates:
        if candidate in slug_map:
            slug = slug_map[candidate]
            break
    if slug:
        return APEXLOL_HEXTECH_IMAGE_URL.format(slug=slug)
    return ""


def _read_augment_icon_source_marker() -> str:
    try:
        with open(AUGMENT_ICON_SOURCE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, IOError):
        return ""


def _write_augment_icon_source_marker(source_id: str) -> None:
    tmp_path = AUGMENT_ICON_SOURCE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(source_id)
    os.replace(tmp_path, AUGMENT_ICON_SOURCE_FILE)


def _find_augment_icon_filename(icon_map: dict, lookup_name: str) -> Optional[str]:
    if not icon_map or not lookup_name:
        return None

    direct = icon_map.get(lookup_name)
    if direct:
        return _normalize_augment_filename(direct)

    normalized_lookup = _normalize_augment_name(lookup_name)
    for key, value in icon_map.items():
        if _normalize_augment_name(key) == normalized_lookup:
            return _normalize_augment_filename(value)
    return None


def _iter_augment_icon_urls(icon_filename: str):
    filename = _normalize_augment_filename(icon_filename)
    templates = [
        # 按优先级尝试多个 CommunityDragon 地址。
        "https://raw.communitydragon.org/latest/game/assets/ux/augments/{filename}",
        "https://raw.communitydragon.org/pbe/game/assets/ux/cherry/augments/icons/{filename}",
        "https://raw.communitydragon.org/pbe/game/assets/ux/augments/{filename}",
    ]
    for template in templates:
        yield template.format(filename=filename)


def _ensure_augment_icon_cached(icon_filename: str, force_refresh: bool = False) -> Optional[str]:
    normalized_filename = _normalize_augment_filename(icon_filename)
    if not normalized_filename:
        return None

    target_path = os.path.join(_assets_dir, normalized_filename)
    if not force_refresh and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return target_path

    tmp_path = target_path + ".tmp"
    for url in _iter_augment_icon_urls(normalized_filename):
        try:
            response = requests.get(url, stream=True, timeout=15)
            if response.status_code != 200:
                continue

            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp_path, target_path)
            return target_path
        except Exception as e:
            logger.debug("Failed to cache augment icon from %s -> %s", url, e)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    return None


def _prefetch_augment_icons(force: bool = False) -> None:
    global _augment_prefetch_mtime

    with _augment_prefetch_lock:
        if not force and _augment_prefetch_mtime:
            return
        _augment_prefetch_mtime = time.time()

    try:
        icon_map = _load_apexlol_hextech_map(force_refresh=force)
        logger.info("已预热 apexlol 海克斯图标映射，共 %s 项", len(icon_map))
    except Exception as e:
        logger.debug("预热 apexlol 海克斯图标映射失败：%s", e)

    if force:
        try:
            _write_augment_icon_source_marker(AUGMENT_ICON_SOURCE_ID)
        except Exception as e:
            logger.debug("Failed to record augment icon source marker: %s", e)



# 请求体：前端点击英雄后发送到 /api/redirect。

class RedirectRequest(BaseModel):
    hero_id: str
    hero_name: str


# PyInstaller 兼容的资源路径解析。

def get_resource_path(relative_path: str) -> str:
    """返回 PyInstaller 兼容的资源路径。"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(__file__), relative_path)


def _html_file_response(filename: str) -> FileResponse:
    """返回 HTML 文件并显式声明 UTF-8，避免浏览器乱码。"""
    return FileResponse(
        os.path.join(_static_dir, filename),
        media_type="text/html; charset=utf-8",
    )


# CSV 文件缓存，减少重复读盘。

@dataclass
class CSVCache:
    path: str = ""
    mtime: float = 0.0
    df: pd.DataFrame = field(default_factory=pd.DataFrame)

_csv_cache = CSVCache()


def get_df() -> pd.DataFrame:
    """返回最新的英雄数据 DataFrame。"""
    latest = get_latest_csv()
    if not latest:
        return pd.DataFrame()
    try:
        current_mtime = os.path.getmtime(latest)
    except OSError:
        return _csv_cache.df

    if latest != _csv_cache.path or current_mtime != _csv_cache.mtime:
        try:
            # 直接读取 CSV，让 pandas 自己推断列类型。
            df = pd.read_csv(latest)
            # 清理列名中的空格，兼容不同来源的 CSV。

            # 动态定位英雄 ID 列。
            id_column = None
            for col_name in ["英雄ID", "英雄 ID"]:
                if col_name in df.columns:
                    id_column = col_name
                    break

            # 将 "123.0" 这类 ID 还原为纯文本。
            if id_column is not None:
                df[id_column] = df[id_column].astype(str).str.strip().str.replace('.0', '', regex=False)

            _csv_cache.path = latest
            _csv_cache.mtime = current_mtime
            _csv_cache.df = df
            logger.info("CSV refreshed: %s", os.path.basename(latest))
        except Exception as e:
            logger.error("CSV refresh failed: %s", e)
            # 读取失败时复用旧缓存，避免页面抖动。
            return _csv_cache.df
    return _csv_cache.df


# JSON 文件缓存。

@dataclass
class JSONFileCache:
    # 记录 JSON 文件的路径、mtime 和解析结果。
    path: str = ""
    mtime: float = 0.0
    data: dict = field(default_factory=dict)

_synergy_cache = JSONFileCache()


def _get_synergy_data() -> dict:
    # 读取并缓存 Champion_Synergy.json。
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
            logger.info("Champion_Synergy.json cache refreshed")
        except Exception as e:
            logger.error("Champion_Synergy.json load failed: %s", e)
            return _synergy_cache.data
    return _synergy_cache.data


# WebSocket 连接管理。

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


# LCU 轮询状态与连接管理。

# 是否在检测到本地锁定英雄时自动跳转详情页。
AUTO_JUMP_ENABLED = True


@dataclass
class LCUState:
    # 保存当前 LCU 连接、会话和英雄选择状态。
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0

_lcu_state = LCUState()


def _create_lcu_session() -> requests.Session:
    # 复用带重试的 Session，降低 LCU 临时失败带来的抖动。
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

# LCU 请求复用 Session。
_lcu_session = _create_lcu_session()


def _scan_lcu_process() -> tuple:
    """扫描 LeagueClientUx.exe 并提取端口与 token。"""
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
    # 忽略本地 LCU 自签名证书告警。
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


async def lcu_polling_loop():
    """持续轮询 LCU 会话。

    - 读取当前可选英雄列表。
    - 找到本地玩家的 championId。
    - 向前端广播选角变化。
    - 连续异常时自动重置连接状态。

    轮询失败时会继续重试，不会让服务退出。


    """
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

                # 收集当前可选英雄 ID。
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

                # 在 myTeam 中按 cellId 找到本地玩家。
                for p in data.get("myTeam", []):
                    if p.get("cellId") == local_cell_id:
                        local_champion_id = p.get("championId")
                        break

                # championId > 0 才表示已经锁定英雄。
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
                            logger.debug("AUTO_JUMP_ENABLED = False; skipping automatic jump broadcast")

            elif res.status_code == 404:
                # LCU 返回 404，说明会话暂时不存在或已切换。
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
                # token 失效或未授权，重新扫描进程并获取新会话。
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


# CSV 变更轮询。

async def csv_watcher_loop():
    """每 3 秒检查一次 CSV 是否更新。

    如果文件发生变化，则向 WebSocket 广播 `data_updated`。

    """

    prev_mtime = 0.0
    while True:
        try:
            # 触发 get_df() 刷新 _csv_cache.mtime。
            get_df()
            current_mtime = _csv_cache.mtime
            if current_mtime > prev_mtime and prev_mtime != 0.0:
                logger.info("CSV 已更新：%s", os.path.basename(_csv_cache.path))
                await manager.broadcast({'type': 'data_updated'})
            prev_mtime = current_mtime
        except (OSError, IOError) as e:
            logger.warning("CSV 监视器错误：%s", e)
        await asyncio.sleep(3)


# FastAPI 生命周期管理。

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时并行执行后台刷新、增强器图标预取和轮询任务。
    scraper_thread = threading.Thread(
        target=refresh_backend_data,
        kwargs={"force": False},
        daemon=True,
        name="backend-refresh-startup",
    )
    scraper_thread.start()
    needs_augment_refresh = _read_augment_icon_source_marker() != AUGMENT_ICON_SOURCE_ID
    augment_thread = threading.Thread(
        target=_prefetch_augment_icons,
        kwargs={"force": needs_augment_refresh},
        daemon=True,
        name="augment-icon-prefetch",
    )
    augment_thread.start()
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

# 挂载 run/static/ 目录，提供前端静态资源。
_static_dir = get_resource_path("static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# 运行时下载的图片和兜底资源都放在 run/assets/。
_assets_dir = get_resource_path("assets")
os.makedirs(_assets_dir, exist_ok=True)
# 这里的资源既包含英雄头像，也包含增强器图标缓存。


# 页面与 API 路由。

@app.get("/")
async def read_index():
    """返回首页 index.html。"""
    return _html_file_response("index.html")

@app.get("/index.html")
async def read_index_explicit():
    """显式访问 /index.html 时也返回首页。"""
    return _html_file_response("index.html")

@app.get("/detail.html")
async def read_detail():
    """返回详情页 detail.html。"""
    return _html_file_response("detail.html")

@app.get("/canvas_fallback.js")
async def read_canvas_fallback():
    """返回 canvas_fallback.js，供 HTML 页面中的图表降级使用。"""
    js_path = os.path.join(_static_dir, "canvas_fallback.js")
    if os.path.exists(js_path):
        return FileResponse(js_path, media_type="application/javascript")
    return JSONResponse(content={"error": "Not found"}, status_code=404)

@app.get("/favicon.ico")
async def favicon():
    """返回空的 favicon 响应，避免 404 噪音。"""
    return Response(status_code=204)

@app.get("/assets/{filename}")
async def get_asset(filename: str):
    """按文件名返回资源；本地不存在时尝试增强器映射或 DDragon 回退。"""

    # 规范化路径，防止目录穿越。




    local_path = os.path.join(_assets_dir, filename)
    # 增强器图标优先走映射表并缓存到本地。
    real_requested = os.path.normcase(os.path.realpath(local_path))
    real_assets_dir = os.path.normcase(os.path.realpath(_assets_dir))
    if not real_requested.startswith(real_assets_dir + os.sep) and real_requested != real_assets_dir:
        logger.warning("Directory traversal blocked: %s -> %s", filename, real_requested)
        return JSONResponse(content={"error": "Forbidden"}, status_code=403)
    if os.path.exists(local_path):
        return FileResponse(local_path)
            # 若映射表没有命中，直接把请求名当作图标文件名。
    if filename.endswith('.png') and not filename[:-4].isdigit():
        try:
            icon_map = _load_augment_icon_map()
            requested_stem = unquote(filename[:-4])
            mapped_filename = _find_augment_icon_filename(icon_map, requested_stem)

            # 未命中时，按原始文件名继续尝试缓存。
            if not mapped_filename:
                mapped_filename = _normalize_augment_filename(filename)

            cached_path = _ensure_augment_icon_cached(mapped_filename)
            if cached_path and os.path.exists(cached_path):
                return FileResponse(cached_path)
        except Exception as e:
            logger.debug("远程资源缓存失败：%s", e)

    # 普通英雄头像尝试从 DDragon CDN 回退。
    if filename.endswith('.png'):
        file_stem = filename[:-4]  # 这里的文件名形如 "123.png"。
        hero_name = get_champion_name(file_stem)
        if hero_name:
            _, en_name = get_champion_info(file_stem)
            if en_name:
                version = _get_ddragon_version()
                ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png"
                return RedirectResponse(url=ddragon_url, status_code=307)
        logger.debug("资源本地不存在，DDragon 回退也失败：%s", filename)

    return JSONResponse(content={"error": "Asset not found"}, status_code=404)

@app.get("/api/champions")
async def api_champions():
    df = get_df()
    return JSONResponse(content=process_champions_data(df))

@app.get("/api/champion/{name}/hextechs")
async def api_champion_hextechs(name: str):
    df = get_df()
    payload = process_hextechs_data(df, name)

    def _rewrite_icons(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            icon_url = _resolve_apexlol_hextech_icon_url(item.get("海克斯名称", ""))
            if icon_url:
                item["icon"] = icon_url

    for key in ("top_10_overall", "comprehensive", "winrate_only", "Prismatic", "Gold", "Silver"):
        _rewrite_icons(payload.get(key))

    return JSONResponse(content=payload)

@app.get("/api/augment_icon_map")
async def api_augment_icon_map():
    # 返回 apexlol 海克斯图标映射，供前端或调试页查找。
    try:
        raw_map = _load_apexlol_hextech_map()
        data = {
            name: APEXLOL_HEXTECH_IMAGE_URL.format(slug=slug)
            for name, slug in raw_map.items()
        }
        return JSONResponse(content=data)
    except Exception as e:
        logger.warning("apexlol 海克斯图标映射读取失败：%s", e)
        return JSONResponse(content={})

@app.get("/api/synergies/{champ_id}")
async def api_synergies(champ_id: str):
    """返回英雄协同数据。"""



    try:
        data = _get_synergy_data()
        if not data:
            return JSONResponse(content={"synergies": []})

        # 先按英雄 ID 精确匹配。
        synergy_data = data.get(champ_id, {})

        # 再尝试通过别名匹配。
        if not synergy_data:
            for key, value in data.items():
                # 先检查 aliases。
                aliases = value.get("aliases", [])
                if champ_id in aliases or champ_id.lower() in [a.lower() for a in aliases]:
                    synergy_data = value
                    break
                # 再检查 key 本身。
                if champ_id.lower() == key.lower():
                    synergy_data = value
                    break

        synergies = synergy_data.get("synergies", []) if synergy_data else []
        return JSONResponse(content={"synergies": synergies})
    except Exception as e:
        logger.warning("协同数据查询失败：%s", e)
        return JSONResponse(content={"synergies": []})

@app.post("/api/redirect")
async def api_redirect(req: RedirectRequest):
    """处理前端点击后的重定向。"""

    # 先尝试从英雄 ID 还原中英文名。
    try:
        hero_name, en_name = get_champion_info(req.hero_id)
    except (ValueError, TypeError):
        # hero_id 不是合法文本时，回退为空字符串。
        hero_name, en_name = '', ''

    # 如果中文名缺失，退回前端传来的名称。
    if not hero_name:
        hero_name = req.hero_name

    # 当前没有 WebSocket 连接时，直接由服务端打开详情页。
    if len(manager.active) == 0:
        url = _build_detail_url(req.hero_id, hero_name or req.hero_name, en_name)
        if _open_managed_browser(url, replace_existing=True):
            return JSONResponse(content={"status": "opened_browser"})
        return JSONResponse(content={"status": "open_browser_failed"}, status_code=500)
    else:
        # 有 WebSocket 在线时，直接广播给前端页面处理。
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
    """从起始端口开始查找可用端口。"""
    import socket

    for port_offset in range(max_attempts):
        port = start_port + port_offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"Could not find available port in range {start_port}-{start_port + max_attempts - 1}")

def _open_chrome(port: int):
    # 打开浏览器访问当前 Web 服务。
    url = f"http://127.0.0.1:{port}"
    _open_managed_browser(url, replace_existing=True)


def run_web_server() -> None:
    global ACTIVE_WEB_PORT

    # 启动时先找可用端口，避免端口占用导致服务直接失败。
    actual_port = find_available_port(SERVER_PORT)
    if actual_port != SERVER_PORT:
        logger.info("端口 %s 已被占用，改用端口 %s", SERVER_PORT, actual_port)

    # 将实际端口写回配置，供 hextech_ui.py 动态读取。
    ACTIVE_WEB_PORT = actual_port
    _write_active_web_port(ACTIVE_WEB_PORT)
    _open_chrome(actual_port)
    uvicorn.run("web_server:app", host="127.0.0.1", port=actual_port, reload=False)


if __name__ == "__main__":
    run_web_server()
