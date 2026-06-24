"""官方本地接口 overlay 候选 provider。

本模块只访问 Riot / LoL 本机接口并把可能出现的三槽候选归一化为 overlay 上游快照。
它不读取凭据文件、不保存 LCU token、不渲染 overlay，也不直接修改游戏客户端。
"""

from __future__ import annotations

import base64
import json
import re
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import psutil
import requests
import urllib3


SLOT_COUNT = 3
STATUS_CANDIDATES_READY = "candidates_ready"       # 已提取完整三槽候选
STATUS_ACTIVE_NO_CANDIDATES = "active_no_candidates"  # 接口可达但未找到候选
STATUS_UNAVAILABLE = "unavailable"                 # 接口不可达（游戏不在选择界面）
STATUS_ERROR = "error"                             # 接口返回错误（如认证失败）

# Live Client Data API（LoL 游戏进程内置 HTTPS 接口，端口 2999）
LIVE_CLIENT_BASE_URL = "https://127.0.0.1:2999"
LIVE_CLIENT_ENDPOINTS = (
    "/liveclientdata/allgamedata",
    "/liveclientdata/activeplayer",
    "/liveclientdata/eventdata",
    "/swagger/v3/openapi.json",
)
# LCU API（LeagueClientUx 进程，端口和 token 从命令行参数提取）
LCU_ENDPOINTS = (
    "/lol-gameflow/v1/gameflow-phase",
    "/lol-gameflow/v1/session",
    "/lol-champ-select/v1/session",
    "/lol-summoner/v1/current-summoner",
)

# JSON 路径探测策略：先在顶层找直接 ID/名称键，再深入容器键递归
_DIRECT_ID_KEYS = ("id", "augmentId", "augment_id", "hextechId", "hextech_id")
_DIRECT_NAME_KEYS = ("name", "displayName", "display_name", "title")
_CONTAINER_KEYS = {
    # 可能包含候选数组的容器键名（已标准化为小写无分隔符）
    "augments",
    "availableaugments",
    "choices",
    "options",
    "selection",
    "current",
    "value",
}
_SELECTED_ONLY_KEYS = {
    # 仅记录已选择/已拥有的增强，不含当前可选候选，跳过以加速搜索
    "pickedaugment",
    "selectedaugment",
    "selectedhextech",
    "augmentselected",
    "hextechselected",
    "previousaugments",
    "ownedaugments",
}
_SLOT_KEY_RE = re.compile(r"^(?:augment|slot|choice|option|hextech)[_-]?([123])$", re.IGNORECASE)


FetchResponse = Callable[[str, dict[str, str]], Any]
CredentialProvider = Callable[[], tuple[str | None, str | None]]


def _clean_text(value: Any, *, limit: int = 160) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _empty_choice(index: int) -> dict[str, Any]:
    return {
        "slot": index,
        "state": "empty",
        "augment_id": "",
        "name": "",
        "confidence": None,
        "diagnostic": "",
    }


def _candidate_snapshot(
    status: str,
    *,
    choices: Sequence[Mapping[str, Any]] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_choices = [dict(choice) for choice in list(choices or [])[:SLOT_COUNT]]
    while len(normalized_choices) < SLOT_COUNT:
        normalized_choices.append(_empty_choice(len(normalized_choices)))
    return {
        "status": status,
        "generated_at": time.time(),
        "choices": normalized_choices,
        "diagnostics": _sanitize_diagnostics(dict(diagnostics or {})),
    }


def _sanitize_diagnostics(value: Any) -> Any:
    """保留诊断形状但避免把 token/header/异常原文带出。"""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _normalize_key(key)
            if "token" in normalized or "auth" in normalized or "password" in normalized or "secret" in normalized:
                sanitized[str(key)] = "<redacted>"
            else:
                sanitized[str(key)] = _sanitize_diagnostics(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_diagnostics(item) for item in value[:20]]
    if isinstance(value, str):
        text = _clean_text(value, limit=160)
        return text if not re.search(r"token|authorization|secret|password", text, re.IGNORECASE) else "<redacted>"
    return value


def _coerce_candidate(raw_value: Any, slot_index: int) -> dict[str, Any] | None:
    if isinstance(raw_value, Mapping):
        augment_id = ""
        name = ""
        for key in _DIRECT_ID_KEYS:
            if key in raw_value:
                augment_id = _clean_text(raw_value.get(key), limit=80)
                break
        for key in _DIRECT_NAME_KEYS:
            if key in raw_value:
                name = _clean_text(raw_value.get(key), limit=80)
                break
        if not augment_id and not name:
            return None
    elif isinstance(raw_value, (str, int, float)):
        name = _clean_text(raw_value, limit=80)
        augment_id = name
        if not name:
            return None
    else:
        return None

    return {
        "slot": slot_index,
        "state": "ready",
        "augment_id": augment_id or name,
        "name": name or augment_id,
        "confidence": 1.0,
        "diagnostic": "",
    }


def _slot_index_from_key(key: Any) -> int | None:
    match = _SLOT_KEY_RE.match(str(key or ""))
    if not match:
        return None
    return int(match.group(1)) - 1


def _extract_slot_keyed_candidates(value: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    slots: dict[int, dict[str, Any]] = {}
    for key, item in value.items():
        slot_index = _slot_index_from_key(key)
        if slot_index is None:
            continue
        candidate = _coerce_candidate(item, slot_index)
        if candidate:
            slots[slot_index] = candidate
    if set(slots) == {0, 1, 2}:
        return [slots[index] for index in range(SLOT_COUNT)]
    return None


def _extract_sequence_candidates(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(list(value)[:SLOT_COUNT]):
        candidate = _coerce_candidate(item, index)
        if not candidate:
            return None
        candidates.append(candidate)
    return candidates if len(candidates) == SLOT_COUNT else None


def _collect_candidate_sets(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    found_paths: list[str] | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    if found_paths is None:
        found_paths = []
    if depth > 8:
        return []

    candidate_sets: list[tuple[str, list[dict[str, Any]]]] = []
    if isinstance(value, Mapping):
        slot_keyed = _extract_slot_keyed_candidates(value)
        if slot_keyed:
            found_paths.append(path)
            candidate_sets.append((path, slot_keyed))
        for key, child in value.items():
            child_path = f"{path}.{key}"
            normalized_key = _normalize_key(key)
            if normalized_key in _SELECTED_ONLY_KEYS:
                continue
            if normalized_key in _CONTAINER_KEYS:
                sequence_candidates = _extract_sequence_candidates(child)
                if sequence_candidates:
                    found_paths.append(child_path)
                    candidate_sets.append((child_path, sequence_candidates))
                if isinstance(child, Mapping):
                    child_slot_keyed = _extract_slot_keyed_candidates(child)
                    if child_slot_keyed:
                        found_paths.append(child_path)
                        candidate_sets.append((child_path, child_slot_keyed))
            candidate_sets.extend(
                _collect_candidate_sets(child, path=child_path, depth=depth + 1, found_paths=found_paths)
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(list(value)[:20]):
            candidate_sets.extend(
                _collect_candidate_sets(child, path=f"{path}[{index}]", depth=depth + 1, found_paths=found_paths)
            )
    return candidate_sets


def extract_official_augment_candidates(payload: Any, *, source: str = "official-api", endpoint: str = "") -> dict[str, Any]:
    """从官方接口响应中提取当前三张候选；只有完整三槽才标记可显示。"""

    if payload is None:
        return _candidate_snapshot(
            STATUS_UNAVAILABLE,
            diagnostics={"source": source, "endpoint": endpoint, "reason": "payload_missing", "field_paths": []},
        )

    found_paths: list[str] = []
    candidate_sets = _collect_candidate_sets(payload, found_paths=found_paths)
    if candidate_sets:
        field_path, choices = candidate_sets[0]
        return _candidate_snapshot(
            STATUS_CANDIDATES_READY,
            choices=choices,
            diagnostics={
                "source": source,
                "endpoint": endpoint,
                "reason": "candidate_fields_found",
                "field_paths": list(dict.fromkeys(found_paths or [field_path])),
            },
        )

    return _candidate_snapshot(
        STATUS_ACTIVE_NO_CANDIDATES,
        diagnostics={"source": source, "endpoint": endpoint, "reason": "candidate_fields_missing", "field_paths": []},
    )


def scan_lcu_process() -> tuple[str | None, str | None]:
    """遍历进程列表，从 LeagueClientUx.exe 命令行提取 --app-port 和 --remoting-auth-token。

    不读取任何凭据文件，不落盘 token，仅从进程命令行参数实时提取。
    """

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


class LcuSnapshotClient:
    """LCU 只作为官方本地状态补充，不把 token 暴露到诊断输出。"""

    def __init__(
        self,
        *,
        credential_provider: CredentialProvider = scan_lcu_process,
        fetch_response: FetchResponse = _default_fetch_response,
        base_url_template: str = "https://127.0.0.1:{port}",
    ) -> None:
        self._credential_provider = credential_provider
        self._fetch_response = fetch_response
        self._base_url_template = base_url_template

    def get_snapshot(self) -> dict[str, Any]:
        port, token = self._credential_provider()
        if not port or not token:
            return _candidate_snapshot(
                STATUS_UNAVAILABLE,
                diagnostics={"source": "lcu", "reason": "lcu_credentials_missing", "endpoints": []},
            )

        auth = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
        headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
        endpoint_statuses: list[dict[str, Any]] = []
        payloads: list[tuple[str, Any]] = []

        for endpoint in LCU_ENDPOINTS:
            url = f"{self._base_url_template.format(port=port)}{endpoint}"
            try:
                response = self._fetch_response(url, headers)
            except Exception as exc:
                endpoint_statuses.append(
                    {"endpoint": endpoint, "status_code": 0, "error_type": exc.__class__.__name__}
                )
                continue
            status_code = int(getattr(response, "status_code", 0) or 0)
            endpoint_statuses.append({"endpoint": endpoint, "status_code": status_code})
            if status_code in (401, 403):
                return _candidate_snapshot(
                    STATUS_ERROR,
                    diagnostics={"source": "lcu", "reason": "unauthorized", "endpoints": endpoint_statuses},
                )
            if status_code == 200:
                try:
                    payloads.append((endpoint, response.json()))
                except ValueError:
                    endpoint_statuses[-1]["error_type"] = "json_decode_failed"

        for endpoint, payload in payloads:
            snapshot = extract_official_augment_candidates(payload, source="lcu", endpoint=endpoint)
            if snapshot["status"] == STATUS_CANDIDATES_READY:
                return snapshot

        if payloads:
            return _candidate_snapshot(
                STATUS_ACTIVE_NO_CANDIDATES,
                diagnostics={"source": "lcu", "reason": "lcu_no_candidates", "endpoints": endpoint_statuses},
            )
        return _candidate_snapshot(
            STATUS_UNAVAILABLE,
            diagnostics={"source": "lcu", "reason": "lcu_unavailable", "endpoints": endpoint_statuses},
        )


class LiveClientDataClient:
    """LoL Live Client Data 本地接口客户端。"""

    def __init__(
        self,
        *,
        fetch_response: FetchResponse = _default_fetch_response,
        base_url: str = LIVE_CLIENT_BASE_URL,
    ) -> None:
        self._fetch_response = fetch_response
        self._base_url = base_url.rstrip("/")

    def get_snapshot(self) -> dict[str, Any]:
        endpoint_statuses: list[dict[str, Any]] = []
        saw_payload = False
        for endpoint in LIVE_CLIENT_ENDPOINTS:
            url = f"{self._base_url}{endpoint}"
            try:
                response = self._fetch_response(url, {"Accept": "application/json"})
            except Exception as exc:
                endpoint_statuses.append(
                    {"endpoint": endpoint, "status_code": 0, "error_type": exc.__class__.__name__}
                )
                continue
            status_code = int(getattr(response, "status_code", 0) or 0)
            endpoint_statuses.append({"endpoint": endpoint, "status_code": status_code})
            if status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                endpoint_statuses[-1]["error_type"] = "json_decode_failed"
                continue
            saw_payload = True
            snapshot = extract_official_augment_candidates(payload, source="live-client-data", endpoint=endpoint)
            if snapshot["status"] == STATUS_CANDIDATES_READY:
                return snapshot

        return _candidate_snapshot(
            STATUS_ACTIVE_NO_CANDIDATES if saw_payload else STATUS_UNAVAILABLE,
            diagnostics={
                "source": "live-client-data",
                "reason": "live_client_no_candidates" if saw_payload else "live_client_unavailable",
                "endpoints": endpoint_statuses,
            },
        )


class OfficialOverlayProvider:
    """官方接口聚合 provider：Live Client Data 优先，LCU 只补状态。"""

    def __init__(
        self,
        *,
        live_client: LiveClientDataClient | None = None,
        lcu_client: LcuSnapshotClient | None = None,
    ) -> None:
        self._live_client = live_client or LiveClientDataClient()
        self._lcu_client = lcu_client or LcuSnapshotClient()

    def get_snapshot(self) -> dict[str, Any]:
        live_snapshot = self._live_client.get_snapshot()
        if live_snapshot["status"] == STATUS_CANDIDATES_READY:
            return live_snapshot

        lcu_snapshot = self._lcu_client.get_snapshot()
        if lcu_snapshot["status"] == STATUS_CANDIDATES_READY:
            return lcu_snapshot

        status = STATUS_ACTIVE_NO_CANDIDATES
        if live_snapshot["status"] == STATUS_ERROR or lcu_snapshot["status"] == STATUS_ERROR:
            status = STATUS_ERROR
        elif live_snapshot["status"] == STATUS_UNAVAILABLE and lcu_snapshot["status"] == STATUS_UNAVAILABLE:
            status = STATUS_UNAVAILABLE

        return _candidate_snapshot(
            status,
            diagnostics={
                "source": "official-api",
                "reason": "official_sources_without_candidates",
                "live_client": live_snapshot.get("diagnostics", {}),
                "lcu": lcu_snapshot.get("diagnostics", {}),
            },
        )


def snapshot_to_debug_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """生成 runtime debug dump；保持脱敏，便于真实 LoL 验收后判断 go/no-go。"""

    return json.loads(json.dumps(_sanitize_diagnostics(dict(snapshot)), ensure_ascii=False))
