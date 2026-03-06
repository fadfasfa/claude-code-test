import asyncio
import base64
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List

import pandas as pd
import psutil
import requests
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from data_processor import process_champions_data, process_hextechs_data
from hextech_query import get_latest_csv
from hero_sync import load_champion_core_data, CONFIG_DIR

# 全局英雄核心数据缓存
_champion_core_cache = None

def get_champion_name(champ_id: int) -> str:
    """根据英雄 ID 获取中文名，使用缓存避免重复加载"""
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logging.warning(f"加载英雄核心数据失败：{e}")
            _champion_core_cache = {}

    champ_id_str = str(champ_id)
    if champ_id_str in _champion_core_cache:
        return _champion_core_cache[champ_id_str].get('name', '')
    return ''

def get_champion_info(champ_id: int) -> tuple:
    """获取英雄 ID 对应的中文名和英文名，返回 (name, en_name)"""
    global _champion_core_cache
    if _champion_core_cache is None:
        try:
            _champion_core_cache = load_champion_core_data()
        except Exception as e:
            logging.warning(f"加载英雄核心数据失败：{e}")
            _champion_core_cache = {}

    champ_id_str = str(champ_id)
    if champ_id_str in _champion_core_cache:
        data = _champion_core_cache[champ_id_str]
        return data.get('name', ''), data.get('en_name', '')
    return '', ''

logging.basicConfig(level=logging.INFO)

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
            for col_name in ['英雄 ID', '英雄 id']:
                if col_name in df.columns:
                    id_column = col_name
                    break

            # 若找到 ID 列，先转换为字符串类型，再执行字符串操作
            if id_column is not None:
                df[id_column] = df[id_column].astype(str).str.strip().str.replace('.0', '', regex=False)

            _csv_cache.path = latest
            _csv_cache.mtime = current_mtime
            _csv_cache.df = df
            logging.info(f"CSV 重新加载成功：{os.path.basename(latest)}")
        except Exception as e:
            logging.error(f"CSV 重新加载失败：{e}")
            # 安全降级：返回上一次缓存的 DataFrame 或空 DataFrame
            return _csv_cache.df
    return _csv_cache.df


# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()


# ── LCU polling (async) ───────────────────────────────────────────────────────

# 全局开关变量：防止在未请求的情况下强制广播跳转事件
AUTO_JUMP_ENABLED = True

_lcu_state = {"port": None, "token": None, "current_ids": set(), "local_champ_id": None, "local_champ_name": None, "consecutive_404_count": 0}

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
    urllib3_disable_warnings()
    while True:
        try:
            if not _lcu_state["port"]:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    _lcu_state["port"] = port
                    _lcu_state["token"] = token
                    logging.info(f"检测到 LCU 进程：port={port}")
                else:
                    await asyncio.sleep(2)
                    continue

            auth = base64.b64encode(
                f"riot:{_lcu_state['token']}".encode()
            ).decode()
            headers = {
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
            }
            url = f"https://127.0.0.1:{_lcu_state['port']}/lol-champ-select/v1/session"

            res = await asyncio.to_thread(
                requests.get, url, headers=headers, verify=False, timeout=3
            )

            if res.status_code == 200:
                data = res.json()
                # 成功响应，重置 404 计数器
                _lcu_state["consecutive_404_count"] = 0

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

                if available_ids != _lcu_state["current_ids"]:
                    _lcu_state["current_ids"] = available_ids.copy()
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
                    prev_champ_id = _lcu_state.get("local_champ_id")

                    if prev_champ_id != local_champion_id:
                        _lcu_state["local_champ_id"] = local_champion_id

                        # 利用 core_data 字典将其转换为英雄中文名和英文名
                        hero_name, en_name = get_champion_info(local_champion_id)
                        _lcu_state["local_champ_name"] = hero_name

                        logging.info(f"本地玩家锁定英雄：{hero_name} (ID={local_champion_id})")

                        # 通过 WebSocket 追加广播精准事件（受 AUTO_JUMP_ENABLED 开关控制）
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast({
                                "type": "local_player_locked",
                                "champion_id": local_champion_id,
                                "hero_name": hero_name,
                                "en_name": en_name,
                            })
                        else:
                            logging.debug(f"AUTO_JUMP_ENABLED = False，已阻止自动跳转广播")

            elif res.status_code == 404:
                # 不在选人阶段，累计 404 错误次数
                _lcu_state["consecutive_404_count"] = _lcu_state.get("consecutive_404_count", 0) + 1

                # 清空上一局的英雄缓存，防止下局选同英雄不触发
                if _lcu_state.get('local_champ_id') is not None:
                    _lcu_state['local_champ_id'] = None
                    _lcu_state['local_champ_name'] = None
                    _lcu_state['current_ids'] = set()

                # 连续 5 次 404 错误，触发自愈重置
                if _lcu_state["consecutive_404_count"] >= 5:
                    logging.warning(f"LCU 连续 {_lcu_state['consecutive_404_count']} 次 404，触发自愈重置端口/令牌")
                    _lcu_state['port'] = None
                    _lcu_state['token'] = None
                    _lcu_state['consecutive_404_count'] = 0

            elif res.status_code in (401, 403):
                # Token 失效，需要重新获取
                logging.warning("LCU Token 失效 (401/403)，重置连接状态")
                _lcu_state['port'] = None
                _lcu_state['token'] = None
            else:
                logging.warning(f"LCU 响应异常：status={res.status_code}，重置端口")
                _lcu_state['port'] = None

        except requests.exceptions.ConnectionError as e:
            # 仅在物理网络断开或进程关闭时才清空端口
            logging.warning(f"LCU 连接断开：{e}")
            _lcu_state["port"] = None
            _lcu_state["token"] = None
        except Exception as e:
            logging.warning(f"LCU 轮询异常：{e}")

        await asyncio.sleep(1.5)

def urllib3_disable_warnings():
    """Suppress urllib3 SSL warnings."""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


# ── CSV file watcher loop ─────────────────────────────────────────────────────

_last_csv_mtime = 0.0

async def csv_watcher_loop():
    """
    Async CSV file watcher loop. Polls the latest CSV file every 3 seconds
    and broadcasts a 'data_updated' message via WebSocket when the file is modified.
    """
    global _last_csv_mtime
    while True:
        try:
            latest = get_latest_csv()
            if latest and os.path.exists(latest):
                current_mtime = os.path.getmtime(latest)
                if current_mtime > _last_csv_mtime and _last_csv_mtime != 0.0:
                    logging.info(f"CSV 文件更新：{os.path.basename(latest)}")
                    await manager.broadcast({'type': 'data_updated'})
                _last_csv_mtime = current_mtime
        except (OSError, IOError) as e:
            logging.warning(f"CSV watcher error: {e}")
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

@app.get("/detail.html")
async def read_detail():
    """Serve detail.html for detail page path."""
    return FileResponse(os.path.join(_static_dir, "detail.html"))

@app.get("/assets/{filename}")
async def get_asset(filename: str):
    """Serve asset files with DDragon CDN fallback if local file is missing."""
    local_path = os.path.join(_assets_dir, filename)
    if os.path.exists(local_path):
        return FileResponse(local_path)
    # File missing, redirect to DDragon official CDN
    # Extract champion name from filename (e.g., "1.png" -> need to lookup)
    # For direct champion images, redirect to ddragon
    if filename.endswith('.png'):
        champ_name = filename[:-4]  # Remove .png extension
        # Try to lookup champion name from core data
        champ_id = champ_name
        champ_name = get_champion_name(champ_id)
        if champ_name:
            en_name = get_champion_info(champ_id)[1]
            if en_name:
                ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/14.3.1/img/champion/{en_name}.png"
                return RedirectResponse(url=ddragon_url, status_code=307)
    # Fallback: return placeholder
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
    json_path = os.path.join(CONFIG_DIR, "Champion_Synergy.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

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
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        logging.warning(f"读取协同数据失败：{e}")
        return JSONResponse(content={"synergies": []})

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=False)
# test