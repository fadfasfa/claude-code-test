"""游戏内 overlay 当前英雄上下文。

本模块只读写 `data/runtime/state/game_overlay_context.v1.json`，用于把桌面端或
本地 LCU 或游戏内 Live Client Data 已明确锁定的当前英雄传给 overlay host。
它只允许访问本机 Riot/LoL HTTPS 接口，不读取 token 文件、不输出 token、不依赖
本地 Web 服务，也不猜测英雄；缺失、损坏、过期或没有英雄 ID 时返回可诊断空上下文。

调用方: overlay.data_source、overlay.lifecycle、dev_checks; 关键依赖: psutil、requests、catalog.version_catalog。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil
import requests
import urllib3

from hextech.client_context import ClientContextProvider, parse_client_context
from hextech.game_context import TypedGameContextProvider
from hextech.catalog.version_catalog import load_champion_core_data
from hextech.overlay.runtime_paths import overlay_runtime_state_path
from hextech.support.atomic_io import atomic_write_json


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CONTEXT_MAX_AGE_SECONDS = 6 * 60 * 60.0
PRESERVE_CONTEXT_ON_MISSING_MAX_AGE_SECONDS = 30.0
DEFAULT_CONTEXT_POLL_INTERVAL_SECONDS = 1.5
LCU_CHAMP_SELECT_SESSION_ENDPOINT = "/lol-champ-select/v1/session"
LIVE_CLIENT_BASE_URL = "https://127.0.0.1:2999"
LIVE_CLIENT_ACTIVE_PLAYER_ENDPOINT = "/liveclientdata/activeplayer"
LIVE_CLIENT_ALL_GAME_DATA_ENDPOINT = "/liveclientdata/allgamedata"


OVERLAY_CONTEXT_FILE = Path(overlay_runtime_state_path("game_overlay_context.v1.json"))

CredentialProvider = Callable[[], tuple[str | None, str | None]]
FetchResponse = Callable[[str, dict[str, str]], Any]
ChampionCoreLoader = Callable[[], Mapping[str, Any]]
ShouldWriteContext = Callable[[], bool]


def _clean_text(value: Any, *, limit: int = 80) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _context_health_value(context: Any) -> str:
    """读取新旧上下文的健康状态，不把 health 枚举值混入连接状态字段。"""

    explicit_health = _clean_text(getattr(getattr(context, "health", None), "value", ""), limit=48)
    if explicit_health:
        return explicit_health
    return {
        "connected": "ready",
        "degraded": "degraded",
        "disconnected": "unavailable",
    }.get(_clean_text(getattr(context, "connection_state", ""), limit=48), "")


def _context_connection_state(context: Any) -> str:
    legacy_state = _clean_text(getattr(context, "connection_state", ""), limit=48)
    if legacy_state:
        return legacy_state
    return {
        "ready": "connected",
        "degraded": "degraded",
        "unavailable": "disconnected",
    }.get(_context_health_value(context), "")


def _empty_context(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": error,
        "generated_at": 0.0,
        "champion_id": "",
        "champion_name": "",
        "source": "",
        "session_id": "",
        "connection_state": "",
        "health": "degraded",
    }


def build_overlay_context_payload(
    *,
    champion_id: Any,
    champion_name: Any,
    source: str,
    teammate_champion_ids: Any = (),
    bench_champion_ids: Any = (),
    phase: str = "",
    connection_state: str = "",
    health: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """构造当前英雄上下文 payload；调用方负责只在明确锁定英雄时传入 ID。"""

    normalized_connection = _clean_text(connection_state, limit=48)
    normalized_health = _clean_text(health, limit=48)
    if not normalized_health:
        normalized_health = {
            "connected": "ready",
            "degraded": "degraded",
            "disconnected": "unavailable",
        }.get(normalized_connection, "")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "champion_id": _clean_text(champion_id, limit=32),
        "champion_name": _clean_text(champion_name, limit=48),
        "source": _clean_text(source, limit=48) or "local",
        "teammate_champion_ids": [
            _clean_text(value, limit=32) for value in teammate_champion_ids if _clean_text(value, limit=32)
        ],
        "bench_champion_ids": [
            _clean_text(value, limit=32) for value in bench_champion_ids if _clean_text(value, limit=32)
        ],
        "phase": _clean_text(phase, limit=48),
        "connection_state": normalized_connection,
        "health": normalized_health,
        "session_id": _clean_text(session_id, limit=64),
    }


def _resolve_context_error(payload: Mapping[str, Any], *, now: float | None = None) -> str:
    explicit_error = _clean_text(payload.get("error"), limit=48)
    if explicit_error:
        return explicit_error
    if payload.get("schema_version") != SCHEMA_VERSION:
        return "schema_mismatch"
    if not _clean_text(payload.get("champion_id"), limit=32):
        return "context_missing"
    try:
        generated_at = float(payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return "context_missing"
    if generated_at <= 0:
        return "context_missing"
    if ((now if now is not None else time.time()) - generated_at) > CONTEXT_MAX_AGE_SECONDS:
        return "context_expired"
    return ""


def read_overlay_context(path: str | Path | None = None) -> dict[str, Any]:
    """读取当前英雄上下文；不可用时不返回旧英雄，避免 overlay 误导。"""

    target = Path(path) if path is not None else OVERLAY_CONTEXT_FILE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return _empty_context("context_missing")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return _empty_context("context_damaged")
    if not isinstance(payload, Mapping):
        return _empty_context("context_damaged")

    error = _resolve_context_error(payload)
    if error:
        empty = _empty_context(error)
        empty["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
        empty["generated_at"] = payload.get("generated_at", 0.0)
        empty["source"] = _clean_text(payload.get("source"), limit=48)
        empty["session_id"] = _clean_text(payload.get("session_id"), limit=64)
        empty["connection_state"] = _clean_text(payload.get("connection_state"), limit=48)
        empty["health"] = _clean_text(payload.get("health"), limit=48) or "degraded"
        if error == "context_unmapped_champion":
            empty["champion_name"] = _clean_text(payload.get("champion_name"), limit=48)
        return empty
    return {
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "ok": True,
        "error": "",
        "generated_at": payload.get("generated_at", 0.0),
        "champion_id": _clean_text(payload.get("champion_id"), limit=32),
        "champion_name": _clean_text(payload.get("champion_name"), limit=48),
        "source": _clean_text(payload.get("source"), limit=48),
        "teammate_champion_ids": list(payload.get("teammate_champion_ids") or []),
        "bench_champion_ids": list(payload.get("bench_champion_ids") or []),
        "phase": _clean_text(payload.get("phase"), limit=48),
        "connection_state": _clean_text(payload.get("connection_state"), limit=48),
        "health": _clean_text(payload.get("health"), limit=48),
        "session_id": _clean_text(payload.get("session_id"), limit=64),
    }


def write_overlay_context(payload: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """原子写入当前英雄上下文。"""

    target = Path(path) if path is not None else OVERLAY_CONTEXT_FILE
    atomic_write_json(target, dict(payload), ensure_ascii=False, indent=2)
    return target


def write_missing_overlay_context(
    path: str | Path | None = None,
    *,
    source: str = "lcu",
    error: str = "context_missing",
    connection_state: str = "",
    health: str = "degraded",
    session_id: str = "",
) -> Path:
    """写入明确的空上下文，避免 overlay 沿用上一位英雄。"""

    payload = _empty_context(error)
    payload["generated_at"] = time.time()
    payload["source"] = _clean_text(source, limit=48) or "local"
    payload["connection_state"] = _clean_text(connection_state, limit=48)
    payload["health"] = _clean_text(health, limit=48) or "degraded"
    payload["session_id"] = _clean_text(session_id, limit=64)
    return write_overlay_context(payload, path)


_PRESERVE_VALID_CONTEXT_ON_MISSING_SOURCES = frozenset(
    {"lcu-unavailable", "lcu-error", "lcu-auth", "lcu-no-session", "lcu-no-champion"}
)


def _has_recent_valid_overlay_context(path: str | Path | None = None) -> bool:
    """短暂保留刚写入的英雄；避免跨局长时间沿用旧 context。"""

    try:
        payload = read_overlay_context(path)
    except Exception:
        logger.debug("读取 overlay 当前英雄上下文失败。", exc_info=True)
        return False
    if not payload.get("ok"):
        return False
    try:
        generated_at = float(payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return (time.time() - generated_at) <= PRESERVE_CONTEXT_ON_MISSING_MAX_AGE_SECONDS


def scan_lcu_process() -> tuple[str | None, str | None]:
    """从 LeagueClientUx.exe 命令行实时提取端口/token；不读凭据文件。"""

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] != "LeagueClientUx.exe":
                continue
            port, token = None, None
            for arg in proc.info["cmdline"] or []:
                if arg.startswith("--app-port="):
                    port = arg.split("=", 1)[1]
                elif arg.startswith("--remoting-auth-token="):
                    token = arg.split("=", 1)[1]
            if port and token:
                return str(port), str(token)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None, None


def _default_fetch_response(url: str, headers: dict[str, str]) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=headers, verify=False, timeout=2.5)


@lru_cache(maxsize=256)
def _resolve_champion_name_cached(champion_key: str) -> str:
    try:
        core_data = load_champion_core_data()
    except Exception:
        logger.debug("加载英雄核心数据失败，LCU 上下文仅写 champion_id。", exc_info=True)
        return ""
    detail = core_data.get(champion_key) if isinstance(core_data, Mapping) else None
    if not isinstance(detail, Mapping):
        return ""
    return _clean_text(detail.get("name") or detail.get("heroName"), limit=48)


def _resolve_champion_name(
    champion_id: Any,
    *,
    core_data_loader: ChampionCoreLoader = load_champion_core_data,
) -> str:
    champion_key = _clean_text(champion_id, limit=32)
    if not champion_key:
        return ""
    if core_data_loader is load_champion_core_data:
        return _resolve_champion_name_cached(champion_key)
    try:
        core_data = core_data_loader()
    except Exception:
        logger.debug("加载英雄核心数据失败，LCU 上下文仅写 champion_id。", exc_info=True)
        return ""
    detail = core_data.get(champion_key) if isinstance(core_data, Mapping) else None
    if not isinstance(detail, Mapping):
        return ""
    return _clean_text(detail.get("name") or detail.get("heroName"), limit=48)


def _normalize_champion_name(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", _clean_text(value, limit=80).casefold())


def _resolve_champion_id_by_name(
    champion_name: Any,
    *,
    core_data_loader: ChampionCoreLoader = load_champion_core_data,
) -> str:
    """用本地英雄核心数据把 2999 返回的英雄名反解为 champion id。"""

    normalized_name = _normalize_champion_name(champion_name)
    if not normalized_name:
        return ""
    try:
        core_data = core_data_loader()
    except Exception:
        logger.debug("加载英雄核心数据失败，Live Client 上下文仅保留名称。", exc_info=True)
        return ""
    if not isinstance(core_data, Mapping):
        return ""
    for champion_id, detail in core_data.items():
        if not isinstance(detail, Mapping):
            continue
        names = (
            detail.get("name"),
            detail.get("title"),
            detail.get("heroName"),
            detail.get("alias"),
            detail.get("en_name"),
            detail.get("enName"),
            detail.get("english_name"),
        )
        for name in names:
            if normalized_name and _normalize_champion_name(name) == normalized_name:
                return _clean_text(champion_id, limit=32)
        aliases = detail.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if normalized_name and _normalize_champion_name(alias) == normalized_name:
                    return _clean_text(champion_id, limit=32)
    return ""


def _extract_live_client_champion_name(payload: Any) -> str:
    """从 2999 activeplayer/allgamedata 响应提取本机英雄名。"""

    if not isinstance(payload, Mapping):
        return ""
    direct_name = _clean_text(payload.get("championName") or payload.get("champion_name"), limit=48)
    if direct_name:
        return direct_name
    active_player = payload.get("activePlayer")
    if isinstance(active_player, Mapping):
        active_name = _clean_text(
            active_player.get("championName") or active_player.get("champion_name"),
            limit=48,
        )
        if active_name:
            return active_name
        active_summoner_name = _clean_text(active_player.get("summonerName"), limit=80)
        all_players = payload.get("allPlayers")
        if active_summoner_name and isinstance(all_players, list):
            for player in all_players:
                if not isinstance(player, Mapping):
                    continue
                if _clean_text(player.get("summonerName"), limit=80) != active_summoner_name:
                    continue
                matched_name = _clean_text(
                    player.get("championName") or player.get("champion_name"),
                    limit=48,
                )
                if matched_name:
                    return matched_name
    return ""


def _write_live_client_context(
    champion_name: str,
    *,
    core_data_loader: ChampionCoreLoader,
    context_path: str | Path | None,
    should_write: ShouldWriteContext | None,
    session_id: str = "",
) -> bool:
    champion_id = _resolve_champion_id_by_name(champion_name, core_data_loader=core_data_loader)
    if should_write is not None and not should_write():
        return False
    if not champion_id:
        payload = _empty_context("context_unmapped_champion")
        payload["generated_at"] = time.time()
        payload["champion_name"] = _clean_text(champion_name, limit=48)
        payload["source"] = "live-client-data"
        payload["session_id"] = _clean_text(session_id, limit=64)
        write_overlay_context(payload, context_path)
        return False
    write_overlay_context(
        build_overlay_context_payload(
            champion_id=champion_id,
            champion_name=champion_name,
            source="live-client-data",
            session_id=session_id,
        ),
        context_path,
    )
    return True


def write_current_live_client_overlay_context_once(
    *,
    fetch_response: FetchResponse = _default_fetch_response,
    core_data_loader: ChampionCoreLoader = load_champion_core_data,
    context_path: str | Path | None = None,
    base_url: str = LIVE_CLIENT_BASE_URL,
    should_write: ShouldWriteContext | None = None,
    session_id: str = "",
) -> bool:
    """轮询一次 2999 Live Client Data；只在能明确映射当前英雄时写有效上下文。"""

    headers = {"Accept": "application/json"}
    for endpoint in (LIVE_CLIENT_ACTIVE_PLAYER_ENDPOINT, LIVE_CLIENT_ALL_GAME_DATA_ENDPOINT):
        url = f"{base_url.rstrip('/')}{endpoint}"
        try:
            response = fetch_response(url, headers)
        except Exception:
            logger.debug("Live Client 当前英雄上下文请求失败：%s", endpoint, exc_info=True)
            continue
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        champion_name = _extract_live_client_champion_name(payload)
        if champion_name:
            return _write_live_client_context(
                champion_name,
                core_data_loader=core_data_loader,
                context_path=context_path,
                should_write=should_write,
                session_id=session_id,
            )
    return False


def _extract_local_champion_id(session_payload: Mapping[str, Any]) -> int | None:
    champion_id = parse_client_context(session_payload).local_champion_id
    try:
        value = int(champion_id or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def write_current_lcu_overlay_context_once(
    *,
    credential_provider: CredentialProvider = scan_lcu_process,
    fetch_response: FetchResponse = _default_fetch_response,
    core_data_loader: ChampionCoreLoader = load_champion_core_data,
    context_path: str | Path | None = None,
    base_url_template: str = "https://127.0.0.1:{port}",
    should_write: ShouldWriteContext | None = None,
    context_provider: ClientContextProvider | TypedGameContextProvider | None = None,
) -> bool:
    """轮询一次本机 LCU 当前英雄；只有明确 championId 时才刷新上下文。"""

    def write_missing_if_allowed(source: str) -> bool:
        if should_write is not None and not should_write():
            return False
        if context_provider is not None:
            if source == "lcu-no-session":
                degraded = context_provider.not_in_champ_select(source="overlay-lcu")
            else:
                degraded = context_provider.unavailable(source, source="overlay-lcu")
            if degraded.local_champion_id:
                champion_name = _resolve_champion_name(
                    int(degraded.local_champion_id),
                    core_data_loader=core_data_loader,
                )
                write_overlay_context(
                    build_overlay_context_payload(
                        champion_id=degraded.local_champion_id,
                        champion_name=champion_name,
                        source="lcu",
                        teammate_champion_ids=degraded.teammate_champion_ids,
                        bench_champion_ids=degraded.bench_champion_ids,
                        phase=degraded.phase,
                        connection_state=_context_connection_state(degraded),
                        health=_context_health_value(degraded),
                        session_id=degraded.session_id,
                    ),
                    context_path,
                )
                return True
        if source in _PRESERVE_VALID_CONTEXT_ON_MISSING_SOURCES:
            if write_current_live_client_overlay_context_once(
                fetch_response=fetch_response,
                core_data_loader=core_data_loader,
                context_path=context_path,
                base_url=LIVE_CLIENT_BASE_URL,
                should_write=should_write,
                session_id=context_provider.session_id if context_provider is not None else "",
            ):
                return True
            # 生产 poller 由 Provider 统一决定旧上下文 TTL；文件窗口只兼容无 Provider 的一次性调用。
            if context_provider is None and _has_recent_valid_overlay_context(context_path):
                return False
            current = read_overlay_context(context_path)
            if current.get("source") == "live-client-data" and current.get("error") == "context_unmapped_champion":
                return False
        if context_provider is not None:
            write_missing_overlay_context(
                context_path,
                source=source,
                error=degraded.error_code or source,
                connection_state=_context_connection_state(degraded),
                health=_context_health_value(degraded),
                session_id=degraded.session_id,
            )
        else:
            connection_state = "disconnected" if source in {"lcu-unavailable", "lcu-error", "lcu-auth"} else ""
            write_missing_overlay_context(
                context_path,
                source=source,
                connection_state=connection_state,
                health="unavailable" if connection_state else "degraded",
            )
        return False

    port, token = credential_provider()
    if not port or not token:
        return write_missing_if_allowed("lcu-unavailable")
    auth = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    url = f"{base_url_template.format(port=port)}{LCU_CHAMP_SELECT_SESSION_ENDPOINT}"
    try:
        response = fetch_response(url, headers)
    except Exception:
        logger.debug("LCU 当前英雄上下文请求失败。", exc_info=True)
        return write_missing_if_allowed("lcu-error")
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in (401, 403):
        logger.debug("LCU 当前英雄上下文认证失效。")
        return write_missing_if_allowed("lcu-auth")
    if status_code != 200:
        source = "lcu-no-session" if status_code == 404 else "lcu-error"
        return write_missing_if_allowed(source)
    try:
        payload = response.json()
    except ValueError:
        return write_missing_if_allowed("lcu-error")
    if not isinstance(payload, Mapping):
        return write_missing_if_allowed("lcu-error")
    client_context = (
        context_provider.update(payload, source="overlay-lcu")
        if context_provider is not None
        else parse_client_context(payload, source="overlay-lcu")
    )
    champion_id = client_context.local_champion_id
    if not champion_id:
        return write_missing_if_allowed(client_context.error_code or "lcu-no-champion")
    champion_name = _resolve_champion_name(int(champion_id), core_data_loader=core_data_loader)
    if should_write is not None and not should_write():
        return False
    write_overlay_context(
        build_overlay_context_payload(
            champion_id=champion_id,
            champion_name=champion_name,
            source="lcu",
            teammate_champion_ids=client_context.teammate_champion_ids,
            bench_champion_ids=client_context.bench_champion_ids,
            phase=client_context.phase,
            connection_state=_context_connection_state(client_context),
            health=_context_health_value(client_context),
            session_id=client_context.session_id,
        ),
        context_path,
    )
    return True


def run_overlay_context_poll_loop(
    stop_event: threading.Event,
    *,
    interval_seconds: float = DEFAULT_CONTEXT_POLL_INTERVAL_SECONDS,
    write_once_func: Callable[[], bool] = write_current_lcu_overlay_context_once,
) -> None:
    """best-effort 常驻轮询；单次失败只降级等待下一轮，不结束 overlay 生命周期。"""

    interval = max(0.2, float(interval_seconds or DEFAULT_CONTEXT_POLL_INTERVAL_SECONDS))
    while not stop_event.is_set():
        try:
            write_once_func()
        except Exception:
            logger.debug("LCU 当前英雄上下文轮询异常。", exc_info=True)
        stop_event.wait(interval)


@dataclass
class OverlayContextPoller:
    """Controller 持有的轮询句柄，便于生命周期停机时收口。"""

    stop_event: threading.Event
    thread: threading.Thread

    def stop(self, timeout_seconds: float = 4.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(0.0, float(timeout_seconds)))


def start_overlay_context_poller(
    *,
    interval_seconds: float = DEFAULT_CONTEXT_POLL_INTERVAL_SECONDS,
    write_once_func: Callable[[], bool] = write_current_lcu_overlay_context_once,
) -> OverlayContextPoller:
    stop_event = threading.Event()
    if write_once_func is write_current_lcu_overlay_context_once:
        context_provider = TypedGameContextProvider()

        def write_current_context() -> bool:
            return write_current_lcu_overlay_context_once(
                should_write=lambda: not stop_event.is_set(),
                context_provider=context_provider,
            )

        write_once_func = write_current_context
    thread = threading.Thread(
        target=run_overlay_context_poll_loop,
        kwargs={
            "stop_event": stop_event,
            "interval_seconds": interval_seconds,
            "write_once_func": write_once_func,
        },
        name="hextech-overlay-context-poller",
        daemon=True,
    )
    thread.start()
    return OverlayContextPoller(stop_event=stop_event, thread=thread)
