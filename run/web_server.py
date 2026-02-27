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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from data_processor import process_champions_data, process_hextechs_data
from hextech_query import get_latest_csv

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
            df = pd.read_csv(latest, dtype={"英雄ID": str})
            df["英雄ID"] = df["英雄ID"].str.strip().str.replace(".0", "", regex=False)
            _csv_cache.path = latest
            _csv_cache.mtime = current_mtime
            _csv_cache.df = df
            logging.info(f"CSV reloaded: {os.path.basename(latest)}")
        except Exception as e:
            logging.error(f"CSV reload failed: {e}")
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

_lcu_state = {"port": None, "token": None, "current_ids": set()}

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
    Async LCU polling loop. Polls LCU session endpoint and broadcasts
    champion ID changes via WebSocket.
    """
    urllib3_disable_warnings()
    while True:
        try:
            if not _lcu_state["port"]:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    _lcu_state["port"] = port
                    _lcu_state["token"] = token
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
            else:
                _lcu_state["port"] = None

        except Exception as e:
            logging.warning(f"LCU poll error: {e}")
            _lcu_state["port"] = None

        await asyncio.sleep(1.5)

def urllib3_disable_warnings():
    """Suppress urllib3 SSL warnings."""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


# ── FastAPI app + lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(lcu_polling_loop())
    yield
    task.cancel()
    try:
        await task
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
app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def read_index():
    """Serve index.html for root path."""
    return FileResponse(os.path.join(_static_dir, "index.html"))

@app.get("/api/champions")
async def api_champions():
    df = get_df()
    return JSONResponse(content=process_champions_data(df))

@app.get("/api/champion/{name}/hextechs")
async def api_champion_hextechs(name: str):
    df = get_df()
    return JSONResponse(content=process_hextechs_data(df, name))

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
