"""桌面 UI 运行时辅助层。

文件职责：
- 承载桌面端后台线程、窗口联动和资源加载等非纯界面逻辑

核心输入：
- `HextechUI` 主类持有的状态、控件和会话对象
- Web live_state、LCU 本地接口和本地图片资源

核心输出：
- 桌面端后台刷新、英雄联动、图片缓存和窗口状态同步

主要依赖：
- `hextech.modules.data.ports.paths`

维护提醒：
- Tk 组件结构仍应留在 `hextech.interfaces.desktop.app`
- 新增后台线程、轮询或资源下载逻辑优先集中在本文件

调用方: dev_checks; 关键依赖: psutil、requests、display.web。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import psutil
import requests
import urllib3
import win32gui
from PIL import Image, ImageDraw, ImageTk

from hextech.modules.game_context.client import ClientContextProvider, parse_client_context
from hextech.interfaces.overlay import context as overlay_context
from hextech.interfaces.overlay.gameflow import probe_lcu_gameflow_in_progress, probe_live_client_in_progress
from hextech.modules.vision.window import find_lol_game_window, is_window_renderable
from hextech.modules.vision.window_titles import LOL_CLIENT_WINDOW_TITLE
from hextech.modules.data.ports.paths import ASSET_DIR, BASE_DIR
from hextech.modules.vision.image_validation import is_valid_png_bytes

if TYPE_CHECKING:
    from hextech.interfaces.desktop.app import HextechUI


logger = logging.getLogger(__name__)
_preload_status_executor: ThreadPoolExecutor | None = None
GAMEFLOW_VISIBILITY_POLL_SECONDS = 1.0
LCU_LOCAL_REQUEST_TIMEOUT_SECONDS = 1.0



# ruff: noqa: E402, F401, F403, F405
from hextech.interfaces.desktop.runtime_processes import *
from hextech.interfaces.desktop.runtime_services import *

def _snapshot_terminal_rows(ui: "HextechUI", *, champion_name: str = "") -> list[dict[str, Any]]:
    """终端兼容层读取固定 generation DTO；Pandas 转换只留在 catalog 查询模块。"""

    snapshot_client = getattr(ui, "_snapshot_client", None)
    if snapshot_client is None:
        return []
    try:
        view = snapshot_client.open_view()
        if champion_name:
            return view.get_champion_augments(champion_name)
        rows: list[dict[str, Any]] = []
        for champion in view.get_champions():
            identity = champion.get("id") or champion.get("name")
            if identity:
                rows.extend(view.get_champion_augments(identity))
        return rows
    except Exception:
        logger.debug("终端兼容查询读取 snapshot 失败。", exc_info=True)
        return []


def run_terminal_loop(ui: "HextechUI") -> None:
    while not ui.stop_event.is_set():
        rows = _snapshot_terminal_rows(ui)
        if rows:
            break
        time.sleep(0.5)
    if not ui.stop_event.is_set():
        _query_terminal().main_query(shared_df=rows, ui_instance=ui)


def _set_click_status(ui: "HextechUI", text: str, color: str) -> None:
    ui._hero_click_status = text
    ui._run_on_ui_thread(lambda: ui._set_status(text, color))


def _refresh_preload_ready(ui: "HextechUI", hero_name: str) -> bool:
    if not _web_frontend_available(ui):
        return False
    normalized_hero = str(hero_name or "").strip()
    if not normalized_hero:
        return False
    web_base = resolve_web_base(ui.web_port_file, timeout=1.0)
    try:
        response = requests.get(
            f"{web_base}/api/champion/{quote(normalized_hero)}/preload_status",
            headers=_web_auth_headers(ui, web_base, timeout=0.2),
            timeout=1.0,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        is_ready = bool(payload.get("ready"))
        with ui._hero_preload_lock:
            ui._hero_preload_ready[normalized_hero] = is_ready
            if payload.get("pending"):
                ui._hero_preload_pending.add(normalized_hero)
            else:
                ui._hero_preload_pending.discard(normalized_hero)
        return is_ready
    except Exception:
        logger.debug("刷新英雄预热状态失败：hero=%s", normalized_hero, exc_info=True)
        return False


def _record_redirect_success(ui: "HextechUI", web_base: str) -> None:
    ui._last_redirect_success_base = web_base
    ui._last_redirect_success_at = time.time()


def _resolve_redirect_base(ui: "HextechUI") -> str:
    if ui._last_redirect_success_base and (time.time() - ui._last_redirect_success_at) < 60.0:
        return ui._last_redirect_success_base
    return resolve_web_base(ui.web_port_file, timeout=1.0)


def _store_live_state_marker(ui: "HextechUI", payload: dict, source: str) -> None:
    ui._last_live_state_version = int(payload.get("state_version", -1) or -1)
    ui._last_live_state_updated_at = float(payload.get("updated_at", 0.0) or 0.0)
    ui._last_live_state_source = source


def _is_newer_live_state(ui: "HextechUI", payload: dict, source: str) -> bool:
    state_version = int(payload.get("state_version", -1) or -1)
    updated_at = float(payload.get("updated_at", 0.0) or 0.0)
    if source != "web":
        return True
    if state_version > ui._last_live_state_version:
        return True
    if state_version == ui._last_live_state_version and updated_at > ui._last_live_state_updated_at:
        return True
    return False


def _sync_preload_state_for_candidates(ui: "HextechUI", hero_names: list[str]) -> None:
    if not hero_names:
        return
    normalized_names = [str(name).strip() for name in hero_names if str(name).strip()]
    with ui._hero_preload_lock:
        removed_names = [name for name in ui._hero_preload_ready.keys() if name not in normalized_names]
        for name in removed_names:
            ui._hero_preload_ready.pop(name, None)
            ui._hero_preload_pending.discard(name)


def _post_redirect(ui: "HextechUI", web_base: str, champ_id, hero_name, en_name: str) -> bool:
    response = requests.post(
        f"{web_base}/api/redirect",
        json={"hero_id": str(champ_id), "hero_name": hero_name},
        headers=_web_auth_headers(ui, web_base, timeout=0.2),
        timeout=1.5,
    )
    if response.status_code != 200:
        return False
    _record_redirect_success(ui, web_base)
    return True


def _open_detail_fallback(web_base: str, champ_id, hero_name: str, en_name: str) -> None:
    if not _is_safe_local_http_base(web_base):
        logger.warning("已拒绝打开非本机详情页地址：%s", web_base)
        return
    url = (
        f"{web_base}/detail.html"
        f"?hero={quote(str(hero_name or ''))}"
        f"&id={quote(str(champ_id or ''))}"
        f"&en={quote(str(en_name or ''))}"
        f"&auto=1"
        f"&detailFirst=1"
    )
    webbrowser.open(url)


def normalize_candidate_groups(candidate_groups) -> dict[str, list[str]]:
    """兼容旧 set/list 输入，并把候选分组收口到稳定 schema。"""

    role_keys = {
        "local_champion_id",
        "teammate_champion_ids",
        "context_phase",
        "context_connection_state",
        "context_error_code",
    }
    if isinstance(candidate_groups, dict):
        selected = candidate_groups.get("selected_champion_ids") or candidate_groups.get("selected") or []
        bench = candidate_groups.get("bench_champion_ids") or candidate_groups.get("bench") or []
    else:
        selected = []
        bench = candidate_groups or []
    selected_ids: list[str] = []
    bench_ids: list[str] = []
    for value in selected:
        _append_unique_champion_id(selected_ids, value)
    for value in bench:
        _append_unique_champion_id(bench_ids, value)
    selected_set = set(selected_ids)
    normalized = {
        "selected_champion_ids": selected_ids,
        "bench_champion_ids": [champion_id for champion_id in bench_ids if champion_id not in selected_set],
    }
    if isinstance(candidate_groups, dict) and role_keys.intersection(candidate_groups):
        local_id = _clean_champion_id(candidate_groups.get("local_champion_id"))
        teammate_ids: list[str] = []
        for value in candidate_groups.get("teammate_champion_ids", []):
            _append_unique_champion_id(teammate_ids, value)
        normalized.update(
            {
                "local_champion_id": local_id,
                "teammate_champion_ids": [value for value in teammate_ids if value != local_id],
                "context_phase": str(candidate_groups.get("context_phase") or ""),
                "context_connection_state": str(candidate_groups.get("context_connection_state") or ""),
                "context_error_code": str(candidate_groups.get("context_error_code") or ""),
            }
        )
    return normalized


def _resolve_candidate_hero_names(ui: "HextechUI", candidate_groups) -> list[str]:
    hero_names = []
    normalized = normalize_candidate_groups(candidate_groups)
    for key in ("selected_champion_ids", "bench_champion_ids"):
        for hero_id in normalized[key]:
            core_entry = ui.core_data.get(str(hero_id), {}) if isinstance(ui.core_data, dict) else {}
            hero_name = str(core_entry.get("name", "")).strip()
            if hero_name and hero_name not in hero_names:
                hero_names.append(hero_name)
    return hero_names


def _apply_candidate_update(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    if payload and not _is_newer_live_state(ui, payload, source):
        return
    if payload:
        _store_live_state_marker(ui, payload, source)
        _write_overlay_context_from_live_state(ui, payload, source=source)
    normalized_groups = normalize_candidate_groups(candidate_groups)
    available_ids = _candidate_groups_to_id_set(normalized_groups)
    hero_names = _resolve_candidate_hero_names(ui, normalized_groups)
    _sync_preload_state_for_candidates(ui, hero_names)
    if available_ids != ui.current_hero_ids or normalized_groups != getattr(ui, "current_candidate_groups", {}):
        ui.current_hero_ids = available_ids.copy()
        ui.current_candidate_groups = normalized_groups
        if hero_names:
            _queue_ui_preload(ui, hero_names)
        ui.root.after(0, ui.update_ui, normalized_groups)
    elif hero_names:
        _queue_ui_preload(ui, hero_names)


def _fetch_web_live_state(ui: "HextechUI") -> tuple[dict[str, list[str]] | None, dict | None]:
    if not _web_frontend_available(ui):
        return None, None
    web_base = _resolve_redirect_base(ui)
    response = ui.session.get(f"{web_base}/api/live_state", headers=_web_auth_headers(ui, web_base), timeout=2)
    if response.status_code != 200:
        return None, None
    payload = response.json()
    candidate_groups = normalize_candidate_groups(
        {
            "selected_champion_ids": payload.get("selected_champion_ids", []),
            "bench_champion_ids": payload.get("bench_champion_ids", payload.get("champion_ids", [])),
            "local_champion_id": payload.get("local_champion_id", ""),
            "teammate_champion_ids": payload.get("teammate_champion_ids", []),
            "context_phase": payload.get("context_phase", ""),
            "context_connection_state": payload.get("context_connection_state", ""),
        }
    )
    web_ids = _candidate_groups_to_id_set(candidate_groups)
    local_champion_id = payload.get("local_champion_id")
    has_local_champion = False
    if isinstance(local_champion_id, int):
        has_local_champion = local_champion_id > 0
    else:
        local_text = str(local_champion_id or "").strip()
        has_local_champion = bool(local_text and local_text != "0")
    if web_ids or has_local_champion:
        return candidate_groups, payload
    return None, None


def _clean_live_champion_id(value) -> str:
    return _clean_champion_id(value)


def _game_overlay_context_writable(ui: "HextechUI", *, context_path: str | os.PathLike[str] | None = None) -> bool:
    """生产 canonical Context 由 Broker 独占；显式路径仅供隔离测试/迁移工具。"""

    if context_path is not None:
        return True
    return False


def _resolve_live_champion_name(ui: "HextechUI", champion_id: str, payload: dict) -> str:
    payload_name = str(payload.get("local_champion_name") or "").strip()
    if payload_name:
        return payload_name
    core_entry = ui.core_data.get(str(champion_id), {}) if isinstance(ui.core_data, dict) else {}
    return str(core_entry.get("name") or "").strip()


def _write_overlay_context_from_live_state(
    ui: "HextechUI",
    payload: dict,
    *,
    source: str,
    context_path: str | os.PathLike[str] | None = None,
) -> bool:
    """同步 overlay 当前英雄上下文；空英雄会写入明确缺失状态。"""

    if not isinstance(payload, dict):
        return False
    if not _game_overlay_context_writable(ui, context_path=context_path):
        return False
    champion_id = _clean_live_champion_id(payload.get("local_champion_id"))
    if not champion_id:
        try:
            overlay_context.write_missing_overlay_context(context_path, source=source)
        except OSError:
            logger.debug("写入 overlay 空英雄上下文失败。", exc_info=True)
        return False
    champion_name = _resolve_live_champion_name(ui, champion_id, payload)
    context_payload = overlay_context.build_overlay_context_payload(
        champion_id=champion_id,
        champion_name=champion_name,
        source=source,
        teammate_champion_ids=payload.get("teammate_champion_ids", []),
        bench_champion_ids=payload.get("bench_champion_ids", []),
        phase=str(payload.get("context_phase") or ""),
        connection_state=str(payload.get("context_connection_state") or ""),
    )
    try:
        overlay_context.write_overlay_context(context_payload, context_path)
    except OSError:
        logger.debug("写入 overlay 英雄上下文失败。", exc_info=True)
        return False
    return True


def _sync_candidate_ids(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    if candidate_groups is None:
        return
    _apply_candidate_update(ui, candidate_groups, source=source, payload=payload)


def _fallback_live_state(ui: "HextechUI") -> dict[str, list[str]] | None:
    return poll_lcu_live_ids(ui)


def _handle_redirect_attempt(ui: "HextechUI", champ_id, hero_name: str, en_name: str) -> bool:
    if not _web_frontend_available(ui):
        return False
    web_base = _resolve_redirect_base(ui)
    try:
        return _post_redirect(ui, web_base, champ_id, hero_name, en_name)
    except Exception:
        logger.debug("请求 /api/redirect 失败，准备重试。", exc_info=True)
        return False


def _drain_preload_pending(ui: "HextechUI") -> None:
    if not _web_frontend_available(ui):
        return
    with ui._hero_preload_lock:
        pending_names = list(ui._hero_preload_pending)
    for hero_name in pending_names:
        _refresh_preload_ready(ui, hero_name)


def _wait_for_redirect_ready(ui: "HextechUI", hero_name: str) -> bool:
    normalized_hero = str(hero_name or "").strip()
    if not normalized_hero:
        return False
    deadline = time.time() + ui._hero_click_gate_timeout
    while time.time() < deadline and not ui.stop_event.is_set():
        if _refresh_preload_ready(ui, normalized_hero):
            return True
        time.sleep(ui._hero_click_gate_poll_interval)
    return _refresh_preload_ready(ui, normalized_hero)


def _normalize_hero_name(hero_name: str) -> str:
    return str(hero_name or "").strip()


def _mark_preload_pending(ui: "HextechUI", hero_names: list[str]) -> None:
    with ui._hero_preload_lock:
        for hero_name in hero_names:
            ui._hero_preload_pending.add(hero_name)
            ui._hero_preload_ready.setdefault(hero_name, False)


def _queue_preload_worker(ui: "HextechUI", hero_names: list[str]) -> None:
    if not _web_frontend_available(ui):
        return
    web_base = _resolve_redirect_base(ui)
    for hero_name in hero_names:
        try:
            requests.post(
                f"{web_base}/api/champion/{quote(hero_name)}/preload",
                headers=_web_auth_headers(ui, web_base, timeout=0.2),
                timeout=1.0,
            )
        except Exception:
            logger.debug("候选英雄预热请求失败：hero=%s", hero_name, exc_info=True)
        _refresh_preload_ready(ui, hero_name)


def _submit_preload(ui: "HextechUI", hero_names: list[str]) -> None:
    _get_preload_status_executor().submit(lambda: _queue_preload_worker(ui, hero_names))


def _queue_ui_preload(ui: "HextechUI", hero_names: list[str]) -> None:
    if not _web_frontend_available(ui):
        return
    normalized_names = []
    for hero_name in hero_names:
        normalized = _normalize_hero_name(hero_name)
        if normalized and normalized not in normalized_names:
            normalized_names.append(normalized)
    if not normalized_names:
        return
    _mark_preload_pending(ui, normalized_names)
    _submit_preload(ui, normalized_names)


def _refresh_clicked_hero_preload(ui: "HextechUI", hero_name: str) -> None:
    _refresh_preload_ready(ui, hero_name)


def _queue_clicked_hero_preload(ui: "HextechUI", hero_name: str) -> None:
    normalized_hero = _normalize_hero_name(hero_name)
    if not normalized_hero:
        return
    _queue_ui_preload(ui, [normalized_hero])
    _get_preload_status_executor().submit(lambda: _refresh_clicked_hero_preload(ui, normalized_hero))


def handle_hero_click(ui: "HextechUI", champ_id, hero_name) -> None:
    try:
        _query_terminal().set_last_hero(hero_name)
    except Exception:
        logger.debug("记录最近一次英雄选择失败。", exc_info=True)

    def terminal_task():
        try:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()
            rows = _snapshot_terminal_rows(ui, champion_name=hero_name)
            query = _query_terminal()
            df_snapshot, _source = query._normalize_query_df(rows)
            query.display_hero_hextech(df_snapshot, hero_name, is_from_ui=True)
        except Exception as exc:
            print(f"\n输出错误: {exc}")

    threading.Thread(target=terminal_task, daemon=True).start()

    def redirect_task():
        normalized_hero = _normalize_hero_name(hero_name)
        if not _web_frontend_available(ui):
            _set_click_status(ui, "Web 前端未启动，已跳过浏览器跳转", "#f9e2af")
            return
        en_name = ui.core_data.get(str(champ_id), {}).get("en_name", "")
        _set_click_status(ui, f"正在跳转 {normalized_hero}...", "#f9e2af")
        _queue_clicked_hero_preload(ui, normalized_hero)
        for _ in range(3):
            if _handle_redirect_attempt(ui, champ_id, hero_name, en_name):
                _set_click_status(ui, f"已跳转 {normalized_hero}，详情数据加载中", "#a6e3a1")
                return
            time.sleep(0.4)
        logger.debug("请求 /api/redirect 多次失败，回退到本地浏览器打开。")
        fallback_base = _resolve_redirect_base(ui)
        _set_click_status(ui, f"本地回退打开 {normalized_hero}", "#f9e2af")
        _open_detail_fallback(fallback_base, champ_id, hero_name, en_name)

    threading.Thread(target=redirect_task, daemon=True).start()




__all__ = [name for name in globals() if not name.startswith("__")]
