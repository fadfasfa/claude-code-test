"""Web LCU 连接、候选英雄状态与广播循环。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.interfaces.web.backend.runtime import *
class ConnectionManager:
    """WebSocket 连接池，负责广播实时事件。"""

    def __init__(self):
        self.active: List = []
        self._lock = asyncio.Lock()
        self._pending_connections = 0
        self.max_connections = 50

    async def connect(self, ws) -> None:
        async with self._lock:
            if len(self.active) + self._pending_connections >= self.max_connections:
                reject = True
            else:
                reject = False
                self._pending_connections += 1
        if reject:
            await ws.close(code=1013, reason="too_many_connections")
            return

        reserved = True
        try:
            await ws.accept()
            async with self._lock:
                self._pending_connections -= 1
                reserved = False
                self.active.append(ws)
        except BaseException:
            if reserved:
                async with self._lock:
                    self._pending_connections -= 1
            raise

    async def disconnect(self, ws) -> None:
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            snapshot = list(self.active)

        async def _send(ws):
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=1.0)
                return None
            except Exception:
                return ws

        dead = [ws for ws in await asyncio.gather(*(_send(ws) for ws in snapshot)) if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)


manager = ConnectionManager()


@dataclass
class LCUState:
    port: Optional[str] = None
    token: Optional[str] = None
    current_ids: Set[str] = field(default_factory=set)
    selected_ids: list[str] = field(default_factory=list)
    bench_ids: list[str] = field(default_factory=list)
    teammate_ids: list[str] = field(default_factory=list)
    context_phase: str = "not_in_champ_select"
    connection_state: str = "disconnected"
    local_champ_id: Optional[int] = None
    local_champ_name: Optional[str] = None
    consecutive_404_count: int = 0
    state_version: int = 0
    updated_at: float = 0.0


_lcu_state = LCUState()
_lcu_state_lock = threading.RLock()
_client_context_provider = ClientContextProvider()


def get_live_state_payload() -> dict:
    with _lcu_state_lock:
        return {
            "champion_ids": sorted(_lcu_state.current_ids),
            "selected_champion_ids": list(_lcu_state.selected_ids),
            "bench_champion_ids": list(_lcu_state.bench_ids),
            "teammate_champion_ids": list(_lcu_state.teammate_ids),
            "context_phase": _lcu_state.context_phase,
            "context_connection_state": _lcu_state.connection_state,
            "local_champion_id": _lcu_state.local_champ_id,
            "local_champion_name": _lcu_state.local_champ_name,
            "state_version": _lcu_state.state_version,
            "updated_at": _lcu_state.updated_at,
        }


def _clean_champion_id(value) -> str:
    text = str(value or "").strip()
    return text if text and text != "0" else ""


def _append_unique_champion_id(target: list[str], value) -> None:
    champion_id = _clean_champion_id(value)
    if champion_id and champion_id not in target:
        target.append(champion_id)


def build_lcu_candidate_groups(payload: dict) -> dict[str, list[str]]:
    """从 LCU champ-select payload 生成 Web/桌面共用候选分组。"""

    from hextech.modules.game_context.client import parse_client_context

    if not isinstance(payload, dict):
        return {"selected_champion_ids": [], "bench_champion_ids": []}
    return parse_client_context(payload).candidate_groups()


def _candidate_groups_to_id_set(candidate_groups: dict[str, list[str]]) -> set[str]:
    return {
        champion_id
        for key in ("selected_champion_ids", "bench_champion_ids")
        for champion_id in candidate_groups.get(key, [])
        if champion_id
    }


def _positive_champion_int(value) -> int | None:
    try:
        champion_id = int(value or 0)
    except (TypeError, ValueError):
        return None
    return champion_id if champion_id > 0 else None


def _extract_lcu_local_champion_id(payload: dict) -> int | None:
    from hextech.modules.game_context.client import parse_client_context

    return _positive_champion_int(parse_client_context(payload).local_champion_id)


def _clear_lcu_local_champion_state() -> bool:
    """清空本机当前英雄；调用方必须持有 _lcu_state_lock。"""

    changed = _lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)
    _lcu_state.local_champ_id = None
    _lcu_state.local_champ_name = None
    if changed:
        _lcu_state.state_version += 1
        _lcu_state.updated_at = time.time()
    return changed


def _clear_lcu_candidate_state(*, clear_local: bool = False) -> bool:
    """清空 LCU 候选池；调用方必须持有 _lcu_state_lock。"""

    changed = bool(
        _lcu_state.current_ids
        or _lcu_state.selected_ids
        or _lcu_state.bench_ids
        or _lcu_state.teammate_ids
    )
    if clear_local:
        changed = changed or _lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)
        _lcu_state.local_champ_id = None
        _lcu_state.local_champ_name = None
    _lcu_state.current_ids = set()
    _lcu_state.selected_ids = []
    _lcu_state.bench_ids = []
    _lcu_state.teammate_ids = []
    if changed:
        _lcu_state.state_version += 1
        _lcu_state.updated_at = time.time()
    return changed


def _apply_client_context(context) -> tuple[dict[str, Any], bool]:
    """把 Provider 结果完整投影到 Web 状态，连接恢复也会推进版本。"""

    candidate_groups = context.candidate_groups(include_roles=True)
    available_ids = _candidate_groups_to_id_set(candidate_groups)
    with _lcu_state_lock:
        clear_local = context.connection_state == "disconnected" or (
            context.connection_state == "connected" and not candidate_groups.get("local_champion_id")
        )
        changed = (
            available_ids != _lcu_state.current_ids
            or candidate_groups["selected_champion_ids"] != _lcu_state.selected_ids
            or candidate_groups["bench_champion_ids"] != _lcu_state.bench_ids
            or candidate_groups["teammate_champion_ids"] != _lcu_state.teammate_ids
            or context.phase != _lcu_state.context_phase
            or context.connection_state != _lcu_state.connection_state
            or (clear_local and (_lcu_state.local_champ_id is not None or bool(_lcu_state.local_champ_name)))
        )
        _lcu_state.current_ids = available_ids.copy()
        _lcu_state.selected_ids = list(candidate_groups["selected_champion_ids"])
        _lcu_state.bench_ids = list(candidate_groups["bench_champion_ids"])
        _lcu_state.teammate_ids = list(candidate_groups["teammate_champion_ids"])
        _lcu_state.context_phase = context.phase
        _lcu_state.connection_state = context.connection_state
        if clear_local:
            _lcu_state.local_champ_id = None
            _lcu_state.local_champ_name = None
        if changed:
            _lcu_state.state_version += 1
            _lcu_state.updated_at = time.time()
    return candidate_groups, changed


def _create_lcu_session() -> requests.Session:
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


_lcu_session = _create_lcu_session()


def _scan_lcu_process() -> tuple:
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


def _log_lcu_tls_warning_once() -> None:
    global _lcu_warning_logged
    if _lcu_warning_logged:
        return
    logger.info("LCU 本地 HTTPS 使用自签名证书，仅对 127.0.0.1 请求关闭证书校验。")
    _lcu_warning_logged = True


def _safe_exception_label(exc: Exception) -> str:
    return exc.__class__.__name__


def _get_lcu_session(url: str, headers: dict) -> requests.Response:
    """LCU 只监听本机自签名 HTTPS，这里局部关闭校验并局部抑制 warning。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        return _lcu_session.get(url, headers=headers, verify=False, timeout=3)


async def lcu_polling_loop() -> None:
    """持续轮询 LCU 选人会话，并把英雄可用集与锁定事件广播给前端。"""
    while True:
        try:
            with _lcu_state_lock:
                current_port = _lcu_state.port
                current_token = _lcu_state.token
            if not current_port:
                port, token = await asyncio.to_thread(_scan_lcu_process)
                if port:
                    with _lcu_state_lock:
                        _lcu_state.port = port
                        _lcu_state.token = token
                    logger.info("已检测到 LCU 连接，端口=%s", port)
                    current_port = port
                    current_token = token
                else:
                    degraded_context = _client_context_provider.unavailable("client_not_found", source="web-lcu")
                    _apply_client_context(degraded_context)
                    if degraded_context.connection_state == "disconnected":
                        with _lcu_state_lock:
                            _clear_lcu_candidate_state(clear_local=True)
                    await asyncio.sleep(2)
                    continue

            if not current_token or not current_port:
                degraded_context = _client_context_provider.unavailable("credentials_missing", source="web-lcu")
                _apply_client_context(degraded_context)
                await asyncio.sleep(2)
                continue
            auth = base64.b64encode(f"riot:{current_token}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
            url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

            _log_lcu_tls_warning_once()
            res = await asyncio.to_thread(_get_lcu_session, url, headers)

            if res.status_code == 200:
                data = res.json()
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count = 0

                client_context = _client_context_provider.update(data, source="web-lcu")
                candidate_groups, state_changed = _apply_client_context(client_context)
                available_ids = _candidate_groups_to_id_set(candidate_groups)
                if state_changed:
                    await manager.broadcast(
                        {
                            "type": "champion_update",
                            "champion_ids": list(available_ids),
                            "selected_champion_ids": list(candidate_groups["selected_champion_ids"]),
                            "bench_champion_ids": list(candidate_groups["bench_champion_ids"]),
                            "local_champion_id": candidate_groups["local_champion_id"],
                            "teammate_champion_ids": list(candidate_groups["teammate_champion_ids"]),
                            "timestamp": time.time(),
                        }
                    )

                local_champion_id = _positive_champion_int(candidate_groups.get("local_champion_id"))

                if local_champion_id is not None:
                    with _lcu_state_lock:
                        prev_champ_id = _lcu_state.local_champ_id
                        hero_name = _lcu_state.local_champ_name or ""
                    if prev_champ_id != local_champion_id:
                        with _lcu_state_lock:
                            _lcu_state.local_champ_id = local_champion_id
                            _lcu_state.state_version += 1
                            _lcu_state.updated_at = time.time()
                        hero_name, en_name = get_champion_info(str(local_champion_id))
                        with _lcu_state_lock:
                            _lcu_state.local_champ_name = hero_name
                            _lcu_state.updated_at = time.time()
                        logger.info("LCU 已锁定英雄：%s (ID=%s)", hero_name, local_champion_id)
                        if AUTO_JUMP_ENABLED:
                            await manager.broadcast(
                                {
                                    "type": "local_player_locked",
                                    "champion_id": local_champion_id,
                                    "hero_name": hero_name,
                                    "en_name": en_name,
                                    "detail_first": True,
                                }
                            )

                else:
                    with _lcu_state_lock:
                        _clear_lcu_local_champion_state()
            elif res.status_code == 404:
                context = _client_context_provider.not_in_champ_select(source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.consecutive_404_count += 1
                    _clear_lcu_candidate_state(clear_local=True)
                    consecutive_404_count = _lcu_state.consecutive_404_count
                    _lcu_state.context_phase = "not_in_champ_select"
                    _lcu_state.connection_state = "connected"
                if consecutive_404_count >= 5:
                    logger.warning("LCU 连续返回 404 五次，重置连接状态（count=%s）", consecutive_404_count)
                    with _lcu_state_lock:
                        _lcu_state.port = None
                        _lcu_state.token = None
                        _lcu_state.consecutive_404_count = 0
                        _clear_lcu_candidate_state(clear_local=True)
            elif res.status_code in (401, 403):
                logger.warning("LCU token 失效或未授权（401/403），重置连接状态。")
                context = _client_context_provider.unavailable("authorization_failed", source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.port = None
                    _lcu_state.token = None
                    if context.connection_state == "disconnected":
                        _clear_lcu_candidate_state(clear_local=True)
            else:
                logger.warning("LCU 响应异常状态码=%s，重置连接状态。", res.status_code)
                context = _client_context_provider.unavailable(f"http_{res.status_code}", source="web-lcu")
                _apply_client_context(context)
                with _lcu_state_lock:
                    _lcu_state.port = None
                    if context.connection_state == "disconnected":
                        _clear_lcu_candidate_state(clear_local=True)
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "LCU 请求异常，已重置连接状态：error_type=%s endpoint=127.0.0.1",
                _safe_exception_label(exc),
            )
            degraded_context = _client_context_provider.unavailable("request_failed", source="web-lcu")
            _apply_client_context(degraded_context)
            with _lcu_state_lock:
                _lcu_state.port = None
                _lcu_state.token = None
                _lcu_state.connection_state = degraded_context.connection_state
                _lcu_state.context_phase = degraded_context.phase
                if degraded_context.connection_state == "disconnected":
                    _clear_lcu_candidate_state(clear_local=True)
        except Exception as exc:
            logger.warning(
                "LCU 轮询异常：error_type=%s context=local_polling_loop",
                _safe_exception_label(exc),
            )
            degraded_context = _client_context_provider.unavailable("unexpected_error", source="web-lcu")
            _apply_client_context(degraded_context)
            with _lcu_state_lock:
                if degraded_context.connection_state == "disconnected":
                    _clear_lcu_candidate_state(clear_local=True)

        await asyncio.sleep(1.5)



__all__ = [name for name in globals() if not name.startswith("__")]
