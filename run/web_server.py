import asyncio
import base64
import json
import logging
import os
import sys
import time
import webbrowser
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

# ── 模块日志（不再重复调用 basicConfig，依赖 hero_sync 的全局配置） ─────────
logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────
SERVER_PORT = int(os.getenv("HEXTECH_PORT", "8000"))
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")

# ── 英雄核心数据缓存 ─────────────────────────────────────────────────────────

_champion_core_cache: Optional[dict] = None


def _ensure_champion_cache() -> dict:
    """确保英雄核心数据缓存已加载，返回缓存字典（消除重复的缓存初始化代码）。"""
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logger.warning(f"加载英雄核心数据失败：{e}")
            _champion_core_cache = {}
    return _champion_core_cache


def get_champion_name(champ_id: str) -> str:
    """根据英雄 ID（字符串）获取中文名，使用缓存避免重复加载。"""
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        return cache[champ_id_str].get('name', '')
    return ''


def get_champion_info(champ_id: str) -> Tuple[str, str]:
    """获取英雄 ID 对应的中文名和英文名，返回 (name, en_name)。"""
    cache = _ensure_champion_cache()
    champ_id_str = str(champ_id)
    if champ_id_str in cache:
        data = cache[champ_id_str]
        return data.get('name', ''), data.get('en_name', '')
    return '', ''


def _get_ddragon_version() -> str:
    """从 config/hero_version.txt 读取当前 DDragon 版本号，读取失败时返回备用版本。"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except (OSError, IOError):
        logger.debug("无法读取 hero_version.txt，使用备用版本号")
    return "14.3.1"


# ── Request models ─────────────────────────────────────────────────────────────

class RedirectRequest(BaseModel):
    hero_id: str
    hero_name: str


# ── Resource path resolution for PyInstaller ──────────────────────────────────

def get_resource_path(relative_path: str) -> str:
    """Get resource path, handling PyInstaller bundled environment."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(__file__), relative_path)


# ── CSV hot-reload cache ──────────────────────────────────────────────────────

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
            # 移除 dtype 强约束，让 pandas 自动推断类型
            df = pd.read_csv(latest)
            df.columns = df.columns.str.replace(' ', '')  # 暴力清除表头所有空格（包括中间空格）

            # 容错遍历：检查移除空格后的列名变体
            id_column = None
            for col_name in ['英雄ID', '英雄id']:
                if col_name in df.columns:
                    id_column = col_name
                    break

            # 若找到 ID 列，先转换为字符串类型，再执行字符串操作
            if id_column is not None:
                df[id_column] = df[id_column].astype(str).str.strip().str.replace('.0', '', regex=False)

            _csv_cache.path = latest
            _csv_cache.mtime = current_mtime
            _csv_cache.df = df
            logger.info(f"CSV 重新加载成功：{os.path.basename(latest)}")
        except Exception as e:
            logger.error(f"CSV 重新加载失败：{e}")
            # 安全降级：返回上一次缓存的 DataFrame 或空 DataFrame
            return _csv_cache.df
    return _csv_cache.df


# ── JSON cache for synergy data ───────────────────────────────────────────────

@dataclass
class JSONFileCache:
    """通用 JSON 文件缓存，基于 mtime 自动重新加载。"""
    path: str = ""
    mtime: float = 0.0
    data: dict = field(default_factory=dict)

_synergy_cache = JSONFileCache()


def _get_synergy_data() -> dict:
    """返回缓存的协同数据，文件更新时自动重新加载。"""
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
            logger.info("Champion_Synergy.json 重新加载成功")
        except Exception as e:
            logger.error(f"Champion_Synergy.json 加载失败：{e}")
            return _synergy_cache.data
    return _synergy_cache.data


# ── WebSocket connection manager ──────────────────────────────────────────────

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
        # 持锁快照，释放后再逐一发送（避免在迭代时列表被 connect/disconnect 修改）
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


# ── LCU polling (async) ───────────────────────────────────────────────────────

# 全局开关变量：防止在未请求的情况下强制广播跳转事件
AUTO_JUMP_ENABLED = True


@dataclass
class LCUState:
    """LCU 连接状态机（替代原始 dict，提供属性访问和类型安全）。"""
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0

_lcu_state = LCUState()


def _create_lcu_session() -> requests.Session:
    """创建带重试策略的 LCU 专用 HTTP Session（连接复用，避免每次轮询握手）。"""
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

# 模块级 LCU 会话复用
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
    """Suppress urllib3 SSL warnings（仅需在启动时调用一次）。"""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


async def lcu_polling_loop():
    """
    Async LCU 轮询循环。轮询 LCU 会话端点并通过 WebSocket 广播英雄 ID 变化。

    新增本地玩家专属追踪：
    - 提取 myTeam 数组中 cellId 等于 localPlayerCellId 的玩家
    - 若该玩家的 championId 大于 0，且与上一次循环的 championId 不同，则广播精准事件
    - 使用状态机变量防止同一英雄重复广播

    自愈机制：
    - 连续 5 次 404 错误后自动重置端口和令牌，重新探测 LCU 进程
    """
    _urllib3_disable_warnings()
    while True:
        try:
            if not _lcu_state.port:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    _lcu_state.port = port
                    _lcu_state.token = token
                    logger.info(f"检测到 LCU 进程：port={port}")
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
                # 成功响应，重置 404 计数器
                _lcu_state.consecutive_404_count = 0

                # ========== 全局可用英雄扫描（原有逻辑） ==========
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

                # ========== 本地玩家英雄锁定精准追踪（新增逻辑） ==========
                local_cell_id = data.get("localPlayerCellId")
                local_champion_id = None

                # 提取 myTeam 数组中 cellId 等于 localPlayerCellId 的玩家
                for p in data.get("myTeam", []):
                    if p.get("cellId") == local_cell_id:
                        local_champion_id = p.get("championId")
                        break

                # 若该玩家的 championId 大于 0，且与上一次循环的 championId 不同
                if local_champion_id and local_champion_id > 0:
                    prev_champ_id = _lcu_state.local_champ_id

                    if prev_champ_id != local_champion_id:
                        _lcu_state.local_champ_id = local_champion_id

                        # 利用 core_data 字典将其转换为英雄中文名和英文名
                        hero_name, en_name = get_champion_info(str(local_champion_id))
                        _lcu_state.local_champ_name = hero_name

                        logger.info(f"本地玩家锁定英雄：{hero_name} (ID={local_champion_id})")

                        # 通过 WebSocket 追加广播精准事件（受 AUTO_JUMP_ENABLED 开关控制）
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast({
                                "type": "local_player_locked",
                                "champion_id": local_champion_id,
                                "hero_name": hero_name,
                                "en_name": en_name,
                            })
                        else:
                            logger.debug("AUTO_JUMP_ENABLED = False，已阻止自动跳转广播")

            elif res.status_code == 404:
                # 不在选人阶段，累计 404 错误次数
                _lcu_state.consecutive_404_count += 1

                # 清空上一局的英雄缓存，防止下局选同英雄不触发
                if _lcu_state.local_champ_id is not None:
                    _lcu_state.local_champ_id = None
                    _lcu_state.local_champ_name = None
                    _lcu_state.current_ids = set()

                # 连续 5 次 404 错误，触发自愈重置
                if _lcu_state.consecutive_404_count >= 5:
                    logger.warning(f"LCU 连续 {_lcu_state.consecutive_404_count} 次 404，触发自愈重置端口/令牌")
                    _lcu_state.port = None
                    _lcu_state.token = None
                    _lcu_state.consecutive_404_count = 0

            elif res.status_code in (401, 403):
                # Token 失效，需要重新获取
                logger.warning("LCU Token 失效 (401/403)，重置连接状态")
                _lcu_state.port = None
                _lcu_state.token = None
            else:
                logger.warning(f"LCU 响应异常：status={res.status_code}，重置端口")
                _lcu_state.port = None

        except requests.exceptions.ConnectionError as e:
            # 仅在物理网络断开或进程关闭时才清空端口
            logger.warning(f"LCU 连接断开：{e}")
            _lcu_state.port = None
            _lcu_state.token = None
        except Exception as e:
            logger.warning(f"LCU 轮询异常：{e}")

        await asyncio.sleep(1.5)


# ── CSV file watcher loop ─────────────────────────────────────────────────────

async def csv_watcher_loop():
    """
    Async CSV file watcher loop. Polls the latest CSV file every 3 seconds
    and broadcasts a 'data_updated' message via WebSocket when the file is modified.

    复用 _csv_cache.mtime 检测变更，不再维护独立的 _last_csv_mtime 全局变量。
    """
    prev_mtime = 0.0
    while True:
        try:
            # 调用 get_df() 触发缓存更新，然后比较 mtime 是否变化
            get_df()
            current_mtime = _csv_cache.mtime
            if current_mtime > prev_mtime and prev_mtime != 0.0:
                logger.info(f"CSV 文件更新：{os.path.basename(_csv_cache.path)}")
                await manager.broadcast({'type': 'data_updated'})
            prev_mtime = current_mtime
        except (OSError, IOError) as e:
            logger.warning(f"CSV watcher error: {e}")
        await asyncio.sleep(3)


# ── FastAPI app + lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
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

# Static files — frontend assets served from run/static/
_static_dir = get_resource_path("static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Assets directory for images and other resources
_assets_dir = get_resource_path("assets")
os.makedirs(_assets_dir, exist_ok=True)
# Note: /assets route is now handled by custom route below for fallback support


# ── API routes ────────────────────────────────────────────────────────────────

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
    """Serve asset files with DDragon CDN fallback if local file is missing.

    安全机制：使用 realpath + normcase 验证请求路径是否在 _assets_dir 内，
    阻止通过 ../ 进行目录遍历攻击（LFI 防御）。
    """
    local_path = os.path.join(_assets_dir, filename)
    # ── LFI 防御：解析真实路径并验证是否在 assets 目录内 ──
    real_requested = os.path.normcase(os.path.realpath(local_path))
    real_assets_dir = os.path.normcase(os.path.realpath(_assets_dir))
    if not real_requested.startswith(real_assets_dir + os.sep) and real_requested != real_assets_dir:
        logger.warning(f"目录遍历攻击被阻断：{filename} -> {real_requested}")
        return JSONResponse(content={"error": "Forbidden"}, status_code=403)
    if os.path.exists(local_path):
        return FileResponse(local_path)
    # File missing, try DDragon CDN fallback for champion images
    if filename.endswith('.png'):
        file_stem = filename[:-4]  # e.g. "123" (英雄 ID)
        hero_name = get_champion_name(file_stem)
        if hero_name:
            _, en_name = get_champion_info(file_stem)
            if en_name:
                version = _get_ddragon_version()
                ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png"
                return RedirectResponse(url=ddragon_url, status_code=307)
        # 无法映射到 CDN，记录日志便于运维排查
        logger.debug(f"本地资源缺失且无法映射到 CDN：{filename}")
    return JSONResponse(content={"error": "Asset not found"}, status_code=404)

@app.get("/api/champions")
async def api_champions():
    df = get_df()
    return JSONResponse(content=process_champions_data(df))

@app.get("/api/champion/{name}/hextechs")
async def api_champion_hextechs(name: str):
    df = get_df()
    return JSONResponse(content=process_hextechs_data(df, name))

@app.get("/api/synergies/{champ_id}")
async def api_synergies(champ_id: str):
    """获取英雄协同数据 API。读取 Champion_Synergy.json 返回对应英雄的 synergies 列表。

    支持 aliases（别名）的模糊匹配支持，确保前端传递名称或 ID 都能准确获取数据。
    """
    try:
        data = _get_synergy_data()
        if not data:
            return JSONResponse(content={"synergies": []})

        # 尝试直接匹配 champ_id
        synergy_data = data.get(champ_id, {})

        # 如果直接匹配失败，尝试别名模糊匹配
        if not synergy_data:
            for key, value in data.items():
                # 检查别名字段
                aliases = value.get("aliases", [])
                if champ_id in aliases or champ_id.lower() in [a.lower() for a in aliases]:
                    synergy_data = value
                    break
                # 检查是否是 ID 与名称的匹配（尝试将 champ_id 与 key 进行模糊匹配）
                if champ_id.lower() == key.lower():
                    synergy_data = value
                    break

        synergies = synergy_data.get("synergies", []) if synergy_data else []
        return JSONResponse(content={"synergies": synergies})
    except Exception as e:
        logger.warning(f"读取协同数据失败：{e}")
        return JSONResponse(content={"synergies": []})

@app.post("/api/redirect")
async def api_redirect(req: RedirectRequest):
    """处理悬浮窗点击英雄的重定向请求。

    根据活跃 WebSocket 连接数决定行为：
    - 无连接时：打开新浏览器窗口
    - 有连接时：广播 local_player_locked 事件触发前端热跳转
    """
    # 获取英雄信息（中文名和英文名）
    try:
        hero_name, en_name = get_champion_info(req.hero_id)
    except (ValueError, TypeError):
        # hero_id 异常，使用空字符串
        hero_name, en_name = '', ''

    # 如果获取不到英雄信息，使用请求中的名称作为后备
    if not hero_name:
        hero_name = req.hero_name

    # 检查 WebSocket 连接池
    if len(manager.active) == 0:
        # 无 WebSocket 连接，打开新浏览器窗口
        url = f"http://127.0.0.1:{SERVER_PORT}/detail.html?hero={req.hero_name}&id={req.hero_id}&en={en_name}&auto=1"
        webbrowser.open(url)
        return JSONResponse(content={"status": "opened_browser"})
    else:
        # 有 WebSocket 连接，广播事件触发前端热跳转
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("web_server:app", host="127.0.0.1", port=SERVER_PORT, reload=False)