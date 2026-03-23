import asyncio
import base64
import json
import logging
import os
import sys
import threading
import time
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

# 鈹€鈹€ 妯″潡鏃ュ織锛堜笉鍐嶉噸澶嶈皟鐢?basicConfig锛屼緷璧?hero_sync 鐨勫叏灞€閰嶇疆锛?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
logger = logging.getLogger(__name__)

# 鈹€鈹€ 甯搁噺 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
SERVER_PORT = int(os.getenv("HEXTECH_PORT", "8000"))
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")
AUGMENT_ICON_SOURCE_FILE = os.path.join(CONFIG_DIR, "augment_icon_source.txt")
AUGMENT_ICON_SOURCE_ID = "communitydragon"

# 鈹€鈹€ 鑻遍泟鏍稿績鏁版嵁缂撳瓨 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

_champion_core_cache: Optional[dict] = None


def _ensure_champion_cache() -> dict:
    """纭繚鑻遍泟鏍稿績鏁版嵁缂撳瓨宸插姞杞斤紝杩斿洖缂撳瓨瀛楀吀锛堟秷闄ら噸澶嶇殑缂撳瓨鍒濆鍖栦唬鐮侊級銆?""
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logger.warning(f"鍔犺浇鑻遍泟鏍稿績鏁版嵁澶辫触锛歿e}")
            _champion_core_cache = {}
    return _champion_core_cache


def get_champion_name(champ_id: str) -> str:
    """鏍规嵁鑻遍泟 ID锛堝瓧绗︿覆锛夎幏鍙栦腑鏂囧悕锛屼娇鐢ㄧ紦瀛橀伩鍏嶉噸澶嶅姞杞姐€?""
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        return cache[champ_id_str].get('name', '')
    return ''


def get_champion_info(champ_id: str) -> Tuple[str, str]:
    """鑾峰彇鑻遍泟 ID 瀵瑰簲鐨勪腑鏂囧悕鍜岃嫳鏂囧悕锛岃繑鍥?(name, en_name)銆?""
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        data = cache[champ_id_str]
        return data.get('name', ''), data.get('en_name', '')
    return '', ''


def _get_ddragon_version() -> str:
    """浠?config/hero_version.txt 璇诲彇褰撳墠 DDragon 鐗堟湰鍙凤紝璇诲彇澶辫触鏃惰繑鍥炲鐢ㄧ増鏈€?""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("鏃犳硶璇诲彇 hero_version.txt锛屼娇鐢ㄥ鐢ㄧ増鏈彿")
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
        logger.warning(f"璇诲彇 Augment_Icon_Map.json 澶辫触锛歿e}")

    return cached_data


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
        # 缁熶竴浣跨敤 CommunityDragon 浣滀负娴峰厠鏂浘鏍囧敮涓€涓婃父锛岄伩鍏嶆贩鐢ㄩ暅鍍忔簮瀵艰嚧椋庢牸涓嶄竴鑷淬€?        "https://raw.communitydragon.org/latest/game/assets/ux/cherry/augments/icons/{filename}",
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
            logger.debug(f"涓嬭浇娴峰厠鏂浘鏍囧け璐ワ細{url} -> {e}")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    return None


def _prefetch_augment_icons(force: bool = False) -> None:
    global _augment_prefetch_mtime

    icon_map_path = os.path.join(CONFIG_DIR, "Augment_Icon_Map.json")
    try:
        current_mtime = os.path.getmtime(icon_map_path)
    except OSError:
        return

    with _augment_prefetch_lock:
        if not force and _augment_prefetch_mtime == current_mtime:
            return
        _augment_prefetch_mtime = current_mtime

    icon_map = _load_augment_icon_map()
    filenames = {
        _normalize_augment_filename(value)
        for value in icon_map.values()
        if _normalize_augment_filename(value)
    }

    if not filenames:
        return

    logger.info(f"寮€濮嬮缂撳瓨娴峰厠鏂浘鏍囷紝鍏?{len(filenames)} 涓?)
    max_workers = min(8, len(filenames))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="augment-cache") as executor:
        futures = {
            executor.submit(_ensure_augment_icon_cached, filename, force): filename
            for filename in sorted(filenames)
        }
        for future in as_completed(futures):
            filename = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.debug(f"棰勭紦瀛樻捣鍏嬫柉鍥炬爣澶辫触锛歿filename} -> {e}")

    if force:
        try:
            _write_augment_icon_source_marker(AUGMENT_ICON_SOURCE_ID)
        except Exception as e:
            logger.debug(f"写入海克斯图标来源标记失败：{e}")



# 鈹€鈹€ Request models 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class RedirectRequest(BaseModel):
    hero_id: str
    hero_name: str


# 鈹€鈹€ Resource path resolution for PyInstaller 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def get_resource_path(relative_path: str) -> str:
    """Get resource path, handling PyInstaller bundled environment."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(__file__), relative_path)


# 鈹€鈹€ CSV hot-reload cache 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@dataclass
class CSVCache:
    path: str = ""
    mtime: float = 0.0
    df: pd.DataFrame = field(default_factory=pd.DataFrame)

_csv_cache = CSVCache()


def get_df() -> pd.DataFrame:
    """Return cached DataFrame, reloading if the CSV file has been modified."""
    latest = get_latest_csv()
    if not latest:
        return pd.DataFrame()
    try:
        current_mtime = os.path.getmtime(latest)
    except OSError:
        return _csv_cache.df

    if latest != _csv_cache.path or current_mtime != _csv_cache.mtime:
        try:
            # 绉婚櫎 dtype 寮虹害鏉燂紝璁?pandas 鑷姩鎺ㄦ柇绫诲瀷
            df = pd.read_csv(latest)
            df.columns = df.columns.str.replace(' ', '')  # 鏆村姏娓呴櫎琛ㄥご鎵€鏈夌┖鏍硷紙鍖呮嫭涓棿绌烘牸锛?

            # 瀹归敊閬嶅巻锛氭鏌ョЩ闄ょ┖鏍煎悗鐨勫垪鍚嶅彉浣?
            id_column = None
            for col_name in ['鑻遍泟ID', '鑻遍泟id']:
                if col_name in df.columns:
                    id_column = col_name
                    break

            # 鑻ユ壘鍒?ID 鍒楋紝鍏堣浆鎹负瀛楃涓茬被鍨嬶紝鍐嶆墽琛屽瓧绗︿覆鎿嶄綔
            if id_column is not None:
                df[id_column] = df[id_column].astype(str).str.strip().str.replace('.0', '', regex=False)

            _csv_cache.path = latest
            _csv_cache.mtime = current_mtime
            _csv_cache.df = df
            logger.info(f"CSV 閲嶆柊鍔犺浇鎴愬姛锛歿os.path.basename(latest)}")
        except Exception as e:
            logger.error(f"CSV 閲嶆柊鍔犺浇澶辫触锛歿e}")
            # 瀹夊叏闄嶇骇锛氳繑鍥炰笂涓€娆＄紦瀛樼殑 DataFrame 鎴栫┖ DataFrame
            return _csv_cache.df
    return _csv_cache.df


# 鈹€鈹€ JSON cache for synergy data 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@dataclass
class JSONFileCache:
    """閫氱敤 JSON 鏂囦欢缂撳瓨锛屽熀浜?mtime 鑷姩閲嶆柊鍔犺浇銆?""
    path: str = ""
    mtime: float = 0.0
    data: dict = field(default_factory=dict)

_synergy_cache = JSONFileCache()


def _get_synergy_data() -> dict:
    """杩斿洖缂撳瓨鐨勫崗鍚屾暟鎹紝鏂囦欢鏇存柊鏃惰嚜鍔ㄩ噸鏂板姞杞姐€?""
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
            logger.info("Champion_Synergy.json 閲嶆柊鍔犺浇鎴愬姛")
        except Exception as e:
            logger.error(f"Champion_Synergy.json 鍔犺浇澶辫触锛歿e}")
            return _synergy_cache.data
    return _synergy_cache.data


# 鈹€鈹€ WebSocket connection manager 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
        # 鎸侀攣蹇収锛岄噴鏀惧悗鍐嶉€愪竴鍙戦€侊紙閬垮厤鍦ㄨ凯浠ｆ椂鍒楄〃琚?connect/disconnect 淇敼锛?
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


# 鈹€鈹€ LCU polling (async) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

# 鍏ㄥ眬寮€鍏冲彉閲忥細闃叉鍦ㄦ湭璇锋眰鐨勬儏鍐典笅寮哄埗骞挎挱璺宠浆浜嬩欢
AUTO_JUMP_ENABLED = True


@dataclass
class LCUState:
    """LCU 杩炴帴鐘舵€佹満锛堟浛浠ｅ師濮?dict锛屾彁渚涘睘鎬ц闂拰绫诲瀷瀹夊叏锛夈€?""
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0

_lcu_state = LCUState()


def _create_lcu_session() -> requests.Session:
    """鍒涘缓甯﹂噸璇曠瓥鐣ョ殑 LCU 涓撶敤 HTTP Session锛堣繛鎺ュ鐢紝閬垮厤姣忔杞鎻℃墜锛夈€?""
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

# 妯″潡绾?LCU 浼氳瘽澶嶇敤
_lcu_session = _create_lcu_session()


def _scan_lcu_process() -> tuple:
    """Blocking psutil scan for LeagueClientUx.exe process."""
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
    """Suppress urllib3 SSL warnings锛堜粎闇€鍦ㄥ惎鍔ㄦ椂璋冪敤涓€娆★級銆?""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


async def lcu_polling_loop():
    """
    Async LCU 杞寰幆銆傝疆璇?LCU 浼氳瘽绔偣骞堕€氳繃 WebSocket 骞挎挱鑻遍泟 ID 鍙樺寲銆?

    鏂板鏈湴鐜╁涓撳睘杩借釜锛?
    - 鎻愬彇 myTeam 鏁扮粍涓?cellId 绛変簬 localPlayerCellId 鐨勭帺瀹?
    - 鑻ヨ鐜╁鐨?championId 澶т簬 0锛屼笖涓庝笂涓€娆″惊鐜殑 championId 涓嶅悓锛屽垯骞挎挱绮惧噯浜嬩欢
    - 浣跨敤鐘舵€佹満鍙橀噺闃叉鍚屼竴鑻遍泟閲嶅骞挎挱

    鑷剤鏈哄埗锛?
    - 杩炵画 5 娆?404 閿欒鍚庤嚜鍔ㄩ噸缃鍙ｅ拰浠ょ墝锛岄噸鏂版帰娴?LCU 杩涚▼
    """
    _urllib3_disable_warnings()
    while True:
        try:
            if not _lcu_state.port:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    _lcu_state.port = port
                    _lcu_state.token = token
                    logger.info(f"妫€娴嬪埌 LCU 杩涚▼锛歱ort={port}")
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
                # 鎴愬姛鍝嶅簲锛岄噸缃?404 璁℃暟鍣?
                _lcu_state.consecutive_404_count = 0

                # ========== 鍏ㄥ眬鍙敤鑻遍泟鎵弿锛堝師鏈夐€昏緫锛?==========
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

                # ========== 鏈湴鐜╁鑻遍泟閿佸畾绮惧噯杩借釜锛堟柊澧為€昏緫锛?==========
                local_cell_id = data.get("localPlayerCellId")
                local_champion_id = None

                # 鎻愬彇 myTeam 鏁扮粍涓?cellId 绛変簬 localPlayerCellId 鐨勭帺瀹?
                for p in data.get("myTeam", []):
                    if p.get("cellId") == local_cell_id:
                        local_champion_id = p.get("championId")
                        break

                # 鑻ヨ鐜╁鐨?championId 澶т簬 0锛屼笖涓庝笂涓€娆″惊鐜殑 championId 涓嶅悓
                if local_champion_id and local_champion_id > 0:
                    prev_champ_id = _lcu_state.local_champ_id

                    if prev_champ_id != local_champion_id:
                        _lcu_state.local_champ_id = local_champion_id

                        # 鍒╃敤 core_data 瀛楀吀灏嗗叾杞崲涓鸿嫳闆勪腑鏂囧悕鍜岃嫳鏂囧悕
                        hero_name, en_name = get_champion_info(str(local_champion_id))
                        _lcu_state.local_champ_name = hero_name

                        logger.info(f"鏈湴鐜╁閿佸畾鑻遍泟锛歿hero_name} (ID={local_champion_id})")

                        # 閫氳繃 WebSocket 杩藉姞骞挎挱绮惧噯浜嬩欢锛堝彈 AUTO_JUMP_ENABLED 寮€鍏虫帶鍒讹級
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast({
                                "type": "local_player_locked",
                                "champion_id": local_champion_id,
                                "hero_name": hero_name,
                                "en_name": en_name,
                            })
                        else:
                            logger.debug("AUTO_JUMP_ENABLED = False锛屽凡闃绘鑷姩璺宠浆骞挎挱")

            elif res.status_code == 404:
                # 涓嶅湪閫変汉闃舵锛岀疮璁?404 閿欒娆℃暟
                _lcu_state.consecutive_404_count += 1

                # 娓呯┖涓婁竴灞€鐨勮嫳闆勭紦瀛橈紝闃叉涓嬪眬閫夊悓鑻遍泟涓嶈Е鍙?
                if _lcu_state.local_champ_id is not None:
                    _lcu_state.local_champ_id = None
                    _lcu_state.local_champ_name = None
                    _lcu_state.current_ids = set()

                # 杩炵画 5 娆?404 閿欒锛岃Е鍙戣嚜鎰堥噸缃?
                if _lcu_state.consecutive_404_count >= 5:
                    logger.warning(f"LCU 杩炵画 {_lcu_state.consecutive_404_count} 娆?404锛岃Е鍙戣嚜鎰堥噸缃鍙?浠ょ墝")
                    _lcu_state.port = None
                    _lcu_state.token = None
                    _lcu_state.consecutive_404_count = 0

            elif res.status_code in (401, 403):
                # Token 澶辨晥锛岄渶瑕侀噸鏂拌幏鍙?
                logger.warning("LCU Token 澶辨晥 (401/403)锛岄噸缃繛鎺ョ姸鎬?)
                _lcu_state.port = None
                _lcu_state.token = None
            else:
                logger.warning(f"LCU 鍝嶅簲寮傚父锛歴tatus={res.status_code}锛岄噸缃鍙?)
                _lcu_state.port = None

        except requests.exceptions.ConnectionError as e:
            # 浠呭湪鐗╃悊缃戠粶鏂紑鎴栬繘绋嬪叧闂椂鎵嶆竻绌虹鍙?
            logger.warning(f"LCU 杩炴帴鏂紑锛歿e}")
            _lcu_state.port = None
            _lcu_state.token = None
        except Exception as e:
            logger.warning(f"LCU 杞寮傚父锛歿e}")

        await asyncio.sleep(1.5)


# 鈹€鈹€ CSV file watcher loop 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

async def csv_watcher_loop():
    """
    Async CSV file watcher loop. Polls the latest CSV file every 3 seconds
    and broadcasts a 'data_updated' message via WebSocket when the file is modified.

    澶嶇敤 _csv_cache.mtime 妫€娴嬪彉鏇达紝涓嶅啀缁存姢鐙珛鐨?_last_csv_mtime 鍏ㄥ眬鍙橀噺銆?
    """
    prev_mtime = 0.0
    while True:
        try:
            # 璋冪敤 get_df() 瑙﹀彂缂撳瓨鏇存柊锛岀劧鍚庢瘮杈?mtime 鏄惁鍙樺寲
            get_df()
            current_mtime = _csv_cache.mtime
            if current_mtime > prev_mtime and prev_mtime != 0.0:
                logger.info(f"CSV 鏂囦欢鏇存柊锛歿os.path.basename(_csv_cache.path)}")
                await manager.broadcast({'type': 'data_updated'})
            prev_mtime = current_mtime
        except (OSError, IOError) as e:
            logger.warning(f"CSV watcher error: {e}")
        await asyncio.sleep(3)


# 鈹€鈹€ FastAPI app + lifespan 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 鍚姩鏃跺悗鍙拌繍琛岀埇铏紙涓嶉樆濉炴湇鍔″惎鍔紝check_execution_permission 闃叉棰戠箒瑙﹀彂锛?
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

# Static files 鈥?frontend assets served from run/static/
_static_dir = get_resource_path("static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Assets directory for images and other resources
_assets_dir = get_resource_path("assets")
os.makedirs(_assets_dir, exist_ok=True)
# Note: /assets route is now handled by custom route below for fallback support


# 鈹€鈹€ API routes 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@app.get("/")
async def read_index():
    """Serve index.html for root path."""
    return FileResponse(os.path.join(_static_dir, "index.html"))

@app.get("/index.html")
async def read_index_explicit():
    """Serve index.html for explicit /index.html path."""
    return FileResponse(os.path.join(_static_dir, "index.html"))

@app.get("/detail.html")
async def read_detail():
    """Serve detail.html for detail page path."""
    return FileResponse(os.path.join(_static_dir, "detail.html"))

@app.get("/canvas_fallback.js")
async def read_canvas_fallback():
    """Serve canvas_fallback.js from static directory (referenced by HTML without /static/ prefix)."""
    js_path = os.path.join(_static_dir, "canvas_fallback.js")
    if os.path.exists(js_path):
        return FileResponse(js_path, media_type="application/javascript")
    return JSONResponse(content={"error": "Not found"}, status_code=404)

@app.get("/favicon.ico")
async def favicon():
    """Return empty 204 for favicon to suppress browser console 404 errors."""
    return Response(status_code=204)

@app.get("/assets/{filename}")
async def get_asset(filename: str):
    """Serve asset files with local caching for augment icons and DDragon fallback for heroes.

    瀹夊叏鏈哄埗锛氫娇鐢?realpath + normcase 楠岃瘉璇锋眰璺緞鏄惁鍦?_assets_dir 鍐咃紝
    闃绘閫氳繃 ../ 杩涜鐩綍閬嶅巻鏀诲嚮锛圠FI 闃插尽锛夈€?

    鏂板锛氭捣鍏嬫柉鍥炬爣鏀寔 - 浼樺厛鏈湴缂撳瓨锛岀己澶辨椂鐢辨湇鍔＄涓嬭浇鍚庡啀鏈湴杩斿洖銆?
    """
    local_path = os.path.join(_assets_dir, filename)
    # 鈹€鈹€ LFI 闃插尽锛氳В鏋愮湡瀹炶矾寰勫苟楠岃瘉鏄惁鍦?assets 鐩綍鍐?鈹€鈹€
    real_requested = os.path.normcase(os.path.realpath(local_path))
    real_assets_dir = os.path.normcase(os.path.realpath(_assets_dir))
    if not real_requested.startswith(real_assets_dir + os.sep) and real_requested != real_assets_dir:
        logger.warning(f"鐩綍閬嶅巻鏀诲嚮琚樆鏂細{filename} -> {real_requested}")
        return JSONResponse(content={"error": "Forbidden"}, status_code=403)
    if os.path.exists(local_path):
        return FileResponse(local_path)
    # File missing, try augment icon cache first.
    if filename.endswith('.png') and not filename[:-4].isdigit():
        try:
            icon_map = _load_augment_icon_map()
            requested_stem = unquote(filename[:-4])
            mapped_filename = _find_augment_icon_filename(icon_map, requested_stem)

            # If the request itself already looks like an icon filename, cache that directly.
            if not mapped_filename:
                mapped_filename = _normalize_augment_filename(filename)

            cached_path = _ensure_augment_icon_cached(mapped_filename)
            if cached_path and os.path.exists(cached_path):
                return FileResponse(cached_path)
        except Exception as e:
            logger.debug(f"娴峰厠鏂浘鏍囨湰鍦扮紦瀛樺け璐ワ細{e}")

    # 鑻遍泟澶村儚澶勭悊 - 鍘熸湁閫昏緫
    if filename.endswith('.png'):
        file_stem = filename[:-4]  # e.g. "123" (鑻遍泟 ID)
        hero_name = get_champion_name(file_stem)
        if hero_name:
            _, en_name = get_champion_info(file_stem)
            if en_name:
                version = _get_ddragon_version()
                ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png"
                return RedirectResponse(url=ddragon_url, status_code=307)
        # 鏃犳硶鏄犲皠鍒?CDN锛岃褰曟棩蹇椾究浜庤繍缁存帓鏌?
        logger.debug(f"鏈湴璧勬簮缂哄け涓旀棤娉曟槧灏勫埌 CDN锛歿filename}")
    return JSONResponse(content={"error": "Asset not found"}, status_code=404)

@app.get("/api/champions")
async def api_champions():
    df = get_df()
    return JSONResponse(content=process_champions_data(df))

@app.get("/api/champion/{name}/hextechs")
async def api_champion_hextechs(name: str):
    df = get_df()
    return JSONResponse(content=process_hextechs_data(df, name))

@app.get("/api/augment_icon_map")
async def api_augment_icon_map():
    """鑾峰彇娴峰厠鏂浘鏍囨槧灏勬枃浠躲€?""
    try:
        data = _load_augment_icon_map()
        return JSONResponse(content=data)
    except Exception as e:
        logger.warning(f"璇诲彇 Augment_Icon_Map.json 澶辫触锛歿e}")
        return JSONResponse(content={})

@app.get("/api/synergies/{champ_id}")
async def api_synergies(champ_id: str):
    """鑾峰彇鑻遍泟鍗忓悓鏁版嵁 API銆傝鍙?Champion_Synergy.json 杩斿洖瀵瑰簲鑻遍泟鐨?synergies 鍒楄〃銆?

    鏀寔 aliases锛堝埆鍚嶏級鐨勬ā绯婂尮閰嶆敮鎸侊紝纭繚鍓嶇浼犻€掑悕绉版垨 ID 閮借兘鍑嗙‘鑾峰彇鏁版嵁銆?
    """
    try:
        data = _get_synergy_data()
        if not data:
            return JSONResponse(content={"synergies": []})

        # 灏濊瘯鐩存帴鍖归厤 champ_id
        synergy_data = data.get(champ_id, {})

        # 濡傛灉鐩存帴鍖归厤澶辫触锛屽皾璇曞埆鍚嶆ā绯婂尮閰?
        if not synergy_data:
            for key, value in data.items():
                # 妫€鏌ュ埆鍚嶅瓧娈?
                aliases = value.get("aliases", [])
                if champ_id in aliases or champ_id.lower() in [a.lower() for a in aliases]:
                    synergy_data = value
                    break
                # 妫€鏌ユ槸鍚︽槸 ID 涓庡悕绉扮殑鍖归厤锛堝皾璇曞皢 champ_id 涓?key 杩涜妯＄硦鍖归厤锛?
                if champ_id.lower() == key.lower():
                    synergy_data = value
                    break

        synergies = synergy_data.get("synergies", []) if synergy_data else []
        return JSONResponse(content={"synergies": synergies})
    except Exception as e:
        logger.warning(f"璇诲彇鍗忓悓鏁版嵁澶辫触锛歿e}")
        return JSONResponse(content={"synergies": []})

@app.post("/api/redirect")
async def api_redirect(req: RedirectRequest):
    """澶勭悊鎮诞绐楃偣鍑昏嫳闆勭殑閲嶅畾鍚戣姹傘€?

    鏍规嵁娲昏穬 WebSocket 杩炴帴鏁板喅瀹氳涓猴細
    - 鏃犺繛鎺ユ椂锛氭墦寮€鏂版祻瑙堝櫒绐楀彛
    - 鏈夎繛鎺ユ椂锛氬箍鎾?local_player_locked 浜嬩欢瑙﹀彂鍓嶇鐑烦杞?
    """
    # 鑾峰彇鑻遍泟淇℃伅锛堜腑鏂囧悕鍜岃嫳鏂囧悕锛?
    try:
        hero_name, en_name = get_champion_info(req.hero_id)
    except (ValueError, TypeError):
        # hero_id 寮傚父锛屼娇鐢ㄧ┖瀛楃涓?
        hero_name, en_name = '', ''

    # 濡傛灉鑾峰彇涓嶅埌鑻遍泟淇℃伅锛屼娇鐢ㄨ姹備腑鐨勫悕绉颁綔涓哄悗澶?
    if not hero_name:
        hero_name = req.hero_name

    # 妫€鏌?WebSocket 杩炴帴姹?
    if len(manager.active) == 0:
        # 鏃?WebSocket 杩炴帴锛屾墦寮€鏂版祻瑙堝櫒绐楀彛
        url = f"http://127.0.0.1:{SERVER_PORT}/detail.html?hero={req.hero_name}&id={req.hero_id}&en={en_name}&auto=1"
        webbrowser.open(url)
        return JSONResponse(content={"status": "opened_browser"})
    else:
        # 鏈?WebSocket 杩炴帴锛屽箍鎾簨浠惰Е鍙戝墠绔儹璺宠浆
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


# 鈹€鈹€ Entry point 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def find_available_port(start_port=8000, max_attempts=50):
    """Find an available port starting from start_port."""
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
    """鍦ㄧ郴缁熼粯璁ゆ祻瑙堝櫒涓墦寮€搴旂敤锛屽鐢ㄧ幇鏈夌敤鎴蜂細璇濄€?""
    url = f"http://127.0.0.1:{port}"
    try:
        # 浣跨敤绯荤粺榛樿娴忚鍣紝澶嶇敤鐜版湁鐢ㄦ埛浼氳瘽
        webbrowser.open(url)
        logger.info(f"宸插湪榛樿娴忚鍣ㄤ腑鎵撳紑: {url}")
    except Exception as e:
        logger.warning(f"鏃犳硶鎵撳紑榛樿娴忚鍣? {e}")


if __name__ == "__main__":
    # 鍦ㄥ惎鍔ㄦ湇鍔″櫒鍓嶆壘鍒板彲鐢ㄧ鍙?
    actual_port = find_available_port(SERVER_PORT)
    if actual_port != SERVER_PORT:
        logger.info(f"Port {SERVER_PORT} is occupied, using port {actual_port} instead")

    # 鍦ㄥ惎鍔ㄦ湇鍔″櫒鍓嶆墦寮€娴忚鍣?
    _open_chrome(actual_port)
    uvicorn.run("web_server:app", host="127.0.0.1", port=actual_port, reload=False)
