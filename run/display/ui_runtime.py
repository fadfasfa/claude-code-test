"""桌面 UI 运行时辅助层。

文件职责：
- 承载桌面端后台线程、窗口联动和资源加载等非纯界面逻辑

核心输入：
- `HextechUI` 主类持有的状态、控件和会话对象
- Web live_state、LCU 本地接口和本地图片资源

核心输出：
- 桌面端后台刷新、英雄联动、图片缓存和窗口状态同步

主要依赖：
- `processing.query_terminal`
- `scraping.version_sync`

维护提醒：
- Tk 组件结构仍应留在 `display.hextech_ui`
- 新增后台线程、轮询或资源下载逻辑优先集中在本文件
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

import psutil
import requests
import win32gui
from PIL import Image, ImageTk

from processing.query_terminal import display_hero_hextech, main_query, set_last_hero
from scraping.version_sync import ASSET_DIR, BASE_DIR

if TYPE_CHECKING:
    from .hextech_ui import HextechUI


logger = logging.getLogger(__name__)
_preload_status_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ui-preload-status")


def _load_server_port() -> int:
    raw_port = str(os.getenv("HEXTECH_PORT", "8000")).strip()
    try:
        port = int(raw_port)
    except ValueError:
        return 8000
    return port if 1024 <= port <= 65535 else 8000


SERVER_PORT = _load_server_port()


def _parse_local_port(raw_port) -> int | None:
    try:
        port = int(str(raw_port or "").strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _is_safe_local_http_base(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and _parse_local_port(parsed.port) is not None


def resolve_web_base(web_port_file: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(web_port_file, "r", encoding="utf-8") as f:
                port = _parse_local_port(f.read())
            if port is not None:
                return f"http://127.0.0.1:{port}"
        except OSError:
            pass
        time.sleep(0.1)
    return f"http://127.0.0.1:{SERVER_PORT}"


def scan_lcu_process() -> tuple:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] == "LeagueClientUx.exe":
                port, token = None, None
                for arg in proc.info["cmdline"] or []:
                    if arg.startswith("--app-port="):
                        port = arg.split("=", 1)[1]
                    if arg.startswith("--remoting-auth-token="):
                        token = arg.split("=", 1)[1]
                if port and token:
                    return port, token
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None, None


def poll_lcu_live_ids(ui: "HextechUI"):
    if not ui._lcu_port or not ui._lcu_token:
        port, token = scan_lcu_process()
        parsed_port = _parse_local_port(port)
        if parsed_port is None or not token:
            ui._lcu_port = None
            ui._lcu_token = None
            return None
        ui._lcu_port = parsed_port
        ui._lcu_token = token

    current_port = _parse_local_port(ui._lcu_port)
    if current_port is None:
        ui._lcu_port = None
        ui._lcu_token = None
        return None

    auth = base64.b64encode(f"riot:{ui._lcu_token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

    try:
        res = ui.session.get(url, headers=headers, verify=False, timeout=2.5)
    except requests.exceptions.RequestException:
        ui._lcu_port = None
        ui._lcu_token = None
        return None

    if res.status_code == 404:
        return set()
    if res.status_code in (401, 403):
        ui._lcu_port = None
        ui._lcu_token = None
        return None
    if res.status_code != 200:
        ui._lcu_port = None
        return None

    try:
        payload = res.json()
    except ValueError:
        return None

    available_ids = {str(c.get("championId")) for c in payload.get("benchChampions", [])}
    local_cell_id = payload.get("localPlayerCellId")
    for player in payload.get("myTeam", []):
        if player.get("cellId") == local_cell_id and player.get("championId"):
            available_ids.add(str(player.get("championId")))
    return {champ_id for champ_id in available_ids if champ_id and champ_id != "0"}


def start_web_server_process(web_port_file: str):
    startupinfo = None
    child_env = os.environ.copy()
    child_env["HEXTECH_BASE_DIR"] = BASE_DIR
    # Desktop startup should open the companion web page automatically.
    # Packaged builds rely on the child web server process to launch the page.
    child_env.pop("HEXTECH_OPEN_BROWSER", None)
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--web-server"]
        cwd = BASE_DIR
    else:
        web_script = os.path.join(BASE_DIR, "web_server.py")
        command = [sys.executable, web_script]
        cwd = BASE_DIR
    web_process = subprocess.Popen(
        command,
        startupinfo=startupinfo,
        cwd=cwd,
        env=child_env,
    )
    resolve_web_base(web_port_file, timeout=5.0)
    return web_process


def initialize_core_threads(ui: "HextechUI") -> None:
    threads = [
        threading.Thread(target=lcu_polling_loop, args=(ui,), daemon=True),
        threading.Thread(target=window_sync_loop, args=(ui,), daemon=True),
        threading.Thread(target=run_terminal_loop, args=(ui,), daemon=True),
    ]
    ui.threads.extend(threads)
    for thread in threads:
        thread.start()


def run_terminal_loop(ui: "HextechUI") -> None:
    while not ui.stop_event.is_set():
        with ui._df_lock:
            is_empty = ui.df.empty
        if not is_empty:
            break
        time.sleep(0.5)
    if not ui.stop_event.is_set():
        with ui._df_lock:
            df_snapshot = ui.df
        main_query(shared_df=df_snapshot, ui_instance=ui)


def run_silent_sync(ui: "HextechUI", refresh_backend_data) -> None:
    """启动阶段执行一次静默刷新，并在完成后把结果回灌到 UI。"""
    try:
        refresh_backend_data(force=False, stop_event=ui.stop_event)
        if ui.stop_event.is_set():
            return
        ui._reload_data_into_ui("数据同步完成", "#a6e3a1")
    except Exception:
        logger.exception("启动阶段后台刷新失败。")
        ui._run_on_ui_thread(lambda: ui._set_status("数据同步失败", "#f38ba8"))


def _set_click_status(ui: "HextechUI", text: str, color: str) -> None:
    ui._hero_click_status = text
    ui._run_on_ui_thread(lambda: ui._set_status(text, color))


def _refresh_preload_ready(ui: "HextechUI", hero_name: str) -> bool:
    normalized_hero = str(hero_name or "").strip()
    if not normalized_hero:
        return False
    web_base = resolve_web_base(ui.web_port_file, timeout=1.0)
    request_token = ui.session.cookies.get("hextech_local_token", "")
    try:
        response = requests.get(
            f"{web_base}/api/champion/{quote(normalized_hero)}/preload_status",
            headers={"Origin": web_base, "X-Hextech-Token": request_token},
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
    request_token = ui.session.cookies.get("hextech_local_token", "")
    response = requests.post(
        f"{web_base}/api/redirect",
        json={"hero_id": str(champ_id), "hero_name": hero_name},
        headers={"Origin": web_base, "X-Hextech-Token": request_token},
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


def _resolve_candidate_hero_names(ui: "HextechUI", available_ids: set[str]) -> list[str]:
    hero_names = []
    for hero_id in available_ids:
        core_entry = ui.core_data.get(str(hero_id), {}) if isinstance(ui.core_data, dict) else {}
        hero_name = str(core_entry.get("name", "")).strip()
        if hero_name and hero_name not in hero_names:
            hero_names.append(hero_name)
    return hero_names


def _apply_candidate_update(ui: "HextechUI", available_ids: set[str], *, source: str, payload: dict | None = None) -> None:
    if payload and not _is_newer_live_state(ui, payload, source):
        return
    if payload:
        _store_live_state_marker(ui, payload, source)
    hero_names = _resolve_candidate_hero_names(ui, available_ids)
    _sync_preload_state_for_candidates(ui, hero_names)
    if available_ids != ui.current_hero_ids:
        ui.current_hero_ids = available_ids.copy()
        if hero_names:
            _queue_ui_preload(ui, hero_names)
        ui.root.after(0, ui.update_ui, available_ids)
    elif hero_names:
        _queue_ui_preload(ui, hero_names)


def _fetch_web_live_state(ui: "HextechUI") -> tuple[set[str] | None, dict | None]:
    web_base = _resolve_redirect_base(ui)
    response = ui.session.get(f"{web_base}/api/live_state", timeout=2)
    if response.status_code != 200:
        return None, None
    payload = response.json()
    web_ids = {str(champ_id) for champ_id in payload.get("champion_ids", []) if str(champ_id).strip()}
    local_champion_id = payload.get("local_champion_id")
    has_local_champion = False
    if isinstance(local_champion_id, int):
        has_local_champion = local_champion_id > 0
    else:
        local_text = str(local_champion_id or "").strip()
        has_local_champion = bool(local_text and local_text != "0")
    if web_ids or has_local_champion:
        return web_ids, payload
    return set(), payload


def _sync_candidate_ids(ui: "HextechUI", available_ids: set[str] | None, *, source: str, payload: dict | None = None) -> None:
    if available_ids is None:
        return
    _apply_candidate_update(ui, available_ids, source=source, payload=payload)


def _fallback_live_state(ui: "HextechUI") -> set[str] | None:
    return poll_lcu_live_ids(ui)


def _handle_redirect_attempt(ui: "HextechUI", champ_id, hero_name: str, en_name: str) -> bool:
    web_base = _resolve_redirect_base(ui)
    try:
        return _post_redirect(ui, web_base, champ_id, hero_name, en_name)
    except Exception:
        logger.debug("请求 /api/redirect 失败，准备重试。", exc_info=True)
        return False


def _drain_preload_pending(ui: "HextechUI") -> None:
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
    web_base = _resolve_redirect_base(ui)
    request_token = ui.session.cookies.get("hextech_local_token", "")
    for hero_name in hero_names:
        try:
            requests.post(
                f"{web_base}/api/champion/{quote(hero_name)}/preload",
                headers={"Origin": web_base, "X-Hextech-Token": request_token},
                timeout=1.0,
            )
        except Exception:
            logger.debug("候选英雄预热请求失败：hero=%s", hero_name, exc_info=True)
        _refresh_preload_ready(ui, hero_name)


def _submit_preload(ui: "HextechUI", hero_names: list[str]) -> None:
    _preload_status_executor.submit(lambda: _queue_preload_worker(ui, hero_names))


def _queue_ui_preload(ui: "HextechUI", hero_names: list[str]) -> None:
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
    _preload_status_executor.submit(lambda: _refresh_clicked_hero_preload(ui, normalized_hero))


def handle_hero_click(ui: "HextechUI", champ_id, hero_name) -> None:
    try:
        set_last_hero(hero_name)
    except Exception:
        logger.debug("记录最近一次英雄选择失败。", exc_info=True)

    def terminal_task():
        try:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()
            with ui._df_lock:
                df_snapshot = ui.df
            display_hero_hextech(df_snapshot, hero_name, is_from_ui=True)
        except Exception as exc:
            print(f"\n输出错误: {exc}")

    threading.Thread(target=terminal_task, daemon=True).start()

    def redirect_task():
        normalized_hero = _normalize_hero_name(hero_name)
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



def lcu_polling_loop(ui: "HextechUI") -> None:
    """优先读取 Web live_state，失败时回退本地 LCU，持续同步可用英雄集合。"""
    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue

        available_ids = None
        payload = None
        try:
            available_ids, payload = _fetch_web_live_state(ui)
        except Exception:
            available_ids = None
            payload = None

        if available_ids is None:
            available_ids = _fallback_live_state(ui)
            payload = None
            source = "lcu"
        else:
            source = "web"

        if available_ids is None:
            available_ids = set()

        _sync_candidate_ids(ui, available_ids, source=source, payload=payload)
        _drain_preload_pending(ui)
        time.sleep(1.5)


def load_and_set_img(ui: "HextechUI", champ_id, label) -> None:
    """加载英雄头像，优先命中本地缓存，缺失时远端下载后回写到本地。"""
    try:
        if not label.winfo_exists():
            return

        def _publish_cached(photo) -> None:
            if label.winfo_exists():
                label.config(image=photo)

        if champ_id in ui.image_cache:
            cached_photo = ui.image_cache[champ_id]
            ui._run_on_ui_thread(lambda p=cached_photo: _publish_cached(p))
            return

        img_path = os.path.join(ASSET_DIR, f"{champ_id}.png")
        if os.path.exists(img_path):
            with Image.open(img_path) as raw_img:
                img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
        else:
            if champ_id in ui.downloading_imgs:
                return
            ui.downloading_imgs.add(champ_id)
            url = f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/{champ_id}.png"
            try:
                res = ui.session.get(url, verify=True, timeout=10)
                if res.status_code != 200:
                    return
                with ui.img_write_lock:
                    with open(img_path, "wb") as f:
                        f.write(res.content)
                with Image.open(BytesIO(res.content)) as raw_img:
                    img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
            finally:
                ui.downloading_imgs.discard(champ_id)

        safe_img = img.copy()

        def _publish_loaded(image_obj=safe_img) -> None:
            photo = ImageTk.PhotoImage(image_obj)
            ui.image_cache[champ_id] = photo
            if label.winfo_exists():
                label.config(image=photo)

        ui._run_on_ui_thread(_publish_loaded)
    except Exception:
        logger.exception("加载英雄头像失败：champ_id=%s", champ_id)


def window_sync_loop(ui: "HextechUI") -> None:
    """根据客户端和游戏窗口状态控制伴生窗口显隐、置顶与持续跟随。"""
    manual_follow_cooldown = 8.0
    hide_grace_seconds = 1.0
    follow_resume_distance = 32
    last_visible_at = 0.0
    last_client_interaction_at = 0.0
    last_client_hwnd = None

    def _foreground_belongs_to_client(hwnd: int | None, foreground_hwnd: int | None) -> bool:
        if not hwnd or not foreground_hwnd:
            return False
        if foreground_hwnd == hwnd:
            return True
        try:
            return win32gui.IsChild(hwnd, foreground_hwnd)
        except Exception:
            return False

    def _has_recent_client_context(now_ts: float) -> bool:
        return (now_ts - last_client_interaction_at) < hide_grace_seconds

    def _target_overlay_position(hwnd_client: int, client_rect: tuple[int, int, int, int]) -> tuple[int, int]:
        try:
            client_area = win32gui.GetClientRect(hwnd_client)
            target_x, target_y = win32gui.ClientToScreen(hwnd_client, (client_area[2], 0))
            return (int(target_x), int(target_y))
        except Exception:
            logger.debug("计算客户端内容区右侧坐标失败，回退到窗口外框。", exc_info=True)
            return (int(client_rect[2]), int(client_rect[1]))

    def _client_rect_jump_detected(current_rect: tuple[int, int, int, int], previous_rect: tuple[int, int, int, int] | None) -> bool:
        if not previous_rect:
            return False
        return (
            abs(current_rect[0] - previous_rect[0]) > follow_resume_distance
            or abs(current_rect[1] - previous_rect[1]) > follow_resume_distance
            or abs(current_rect[2] - previous_rect[2]) > follow_resume_distance
            or abs(current_rect[3] - previous_rect[3]) > follow_resume_distance
        )

    def _update_overlay_position(target_pos: tuple[int, int]) -> None:
        ui._run_on_ui_thread(lambda pos=target_pos: ui._move_overlay_to(pos[0], pos[1]))

    def _should_keep_overlay_visible(client_active: bool, overlay_active: bool, now_ts: float) -> bool:
        return client_active or overlay_active or _has_recent_client_context(now_ts)

    def _set_client_interaction(now_ts: float, hwnd: int | None) -> None:
        nonlocal last_client_interaction_at, last_client_hwnd
        last_client_interaction_at = now_ts
        last_client_hwnd = hwnd

    def _reset_client_tracking() -> None:
        nonlocal last_client_hwnd
        last_client_hwnd = None
        ui._last_client_rect = None

    def _resume_follow_if_ready(client_rect: tuple[int, int, int, int], target_pos: tuple[int, int]) -> None:
        client_jump_detected = _client_rect_jump_detected(client_rect, ui._last_client_rect)
        if client_jump_detected and ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
        if ui._auto_follow_enabled:
            _update_overlay_position(target_pos)
        elif ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
            _update_overlay_position(target_pos)

    def _sync_overlay_follow(hwnd_client: int, client_rect: tuple[int, int, int, int], should_show_overlay: bool) -> None:
        if not should_show_overlay:
            ui._last_client_rect = client_rect
            return
        rect_changed = client_rect != ui._last_client_rect
        target_pos = _target_overlay_position(hwnd_client, client_rect)
        if rect_changed:
            _resume_follow_if_ready(client_rect, target_pos)
        ui._last_client_rect = client_rect

    def _set_overlay_visibility(should_show_overlay: bool, should_keep_topmost: bool, now_ts: float) -> None:
        if should_show_overlay:
            nonlocal last_visible_at
            last_visible_at = now_ts
            ui._show_overlay(topmost=should_keep_topmost)
            return
        if ui._window_visible and (now_ts - last_visible_at) < hide_grace_seconds:
            ui._set_window_topmost(False)
            return
        ui._hide_overlay()

    def _update_client_visibility(now_ts: float, hwnd_client: int | None, client_visible: bool, client_active: bool) -> None:
        if client_visible and client_active:
            _set_client_interaction(now_ts, hwnd_client)
        elif not client_visible:
            _reset_client_tracking()

    def _is_same_client_window(hwnd: int | None) -> bool:
        return bool(hwnd and last_client_hwnd and hwnd == last_client_hwnd)

    def _overlay_active() -> bool:
        return ui._window_visible and is_self_fg

    def _resolve_overlay_policy(client_visible: bool, game_visible: bool, client_active: bool, overlay_active: bool, now_ts: float) -> tuple[bool, bool]:
        if game_visible:
            return False, False
        if not client_visible:
            return False, False
        should_show = _should_keep_overlay_visible(client_active, overlay_active, now_ts)
        return should_show, client_active

    def _sync_for_client(hwnd_client: int | None, client_visible: bool, should_show_overlay: bool) -> None:
        if not client_visible or not hwnd_client:
            _reset_client_tracking()
            if ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
                ui._resume_auto_follow()
            return
        client_rect = win32gui.GetWindowRect(hwnd_client)
        _sync_overlay_follow(hwnd_client, client_rect, should_show_overlay)

    def _client_active(is_client_fg: bool) -> bool:
        return is_client_fg

    def _client_or_overlay_active(is_client_fg: bool, is_self_fg_value: bool) -> tuple[bool, bool]:
        client_active = _client_active(is_client_fg)
        overlay_active = ui._window_visible and is_self_fg_value
        return client_active, overlay_active

    def _resolve_client_visibility(hwnd_client: int | None) -> bool:
        return bool(hwnd_client and win32gui.IsWindowVisible(hwnd_client) and not win32gui.IsIconic(hwnd_client))

    def _resolve_game_visibility(hwnd_game: int | None) -> bool:
        return bool(hwnd_game and win32gui.IsWindowVisible(hwnd_game))

    def _resolve_foreground_title(foreground_hwnd: int | None) -> str:
        return win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else ""

    def _resolve_self_fg(foreground_title: str) -> bool:
        return "Hextech" in foreground_title

    def _resolve_client_fg(hwnd_client: int | None, foreground_hwnd: int | None) -> bool:
        return _foreground_belongs_to_client(hwnd_client, foreground_hwnd)

    def _loop_once(now_ts: float) -> None:
        hwnd_client = win32gui.FindWindow(None, "League of Legends")
        hwnd_game = win32gui.FindWindow(None, "League of Legends (TM) Client")
        fg_window = win32gui.GetForegroundWindow()
        fg_title = _resolve_foreground_title(fg_window)
        is_client_fg = _resolve_client_fg(hwnd_client, fg_window)
        is_self_fg = _resolve_self_fg(fg_title)
        game_visible = _resolve_game_visibility(hwnd_game)
        client_visible = _resolve_client_visibility(hwnd_client)
        client_active, overlay_active = _client_or_overlay_active(is_client_fg, is_self_fg)
        _update_client_visibility(now_ts, hwnd_client, client_visible, client_active)
        should_show_overlay, should_keep_topmost = _resolve_overlay_policy(client_visible, game_visible, client_active, overlay_active, now_ts)
        _set_overlay_visibility(should_show_overlay, should_keep_topmost, now_ts)
        _sync_for_client(hwnd_client, client_visible, should_show_overlay)


    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue
        try:
            now = time.time()
            _loop_once(now)
        except Exception:
            logger.exception("窗口同步循环异常。")
        time.sleep(0.2)

def start_background_scraper(ui: "HextechUI", refresh_backend_data) -> None:
    """启动桌面端后台刷新线程，按固定周期执行自愈和数据同步。"""
    def scraper_loop():
        while not ui.stop_event.is_set():
            try:
                refresh_backend_data(force=False, stop_event=ui.stop_event)
                if ui.stop_event.is_set():
                    return
                ui._reload_data_into_ui("数据同步完成", "#a6e3a1")
            except Exception:
                logger.exception("定时后台刷新失败。")
                ui._run_on_ui_thread(lambda: ui._set_status("后台刷新失败", "#f38ba8"))

            for _ in range(14400):
                if ui.stop_event.is_set():
                    break
                time.sleep(1)

    threading.Thread(target=scraper_loop, daemon=True).start()
