"""独立 DataService 的串行 action 内核。

本模块先提供不依赖 UI/Web/Overlay 的业务内核；进程控制面只负责调用这些 action。
refresh 与私用统计切换共用一把锁，避免旧 refresh 在稍后覆盖新策略。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path

import psutil

from hextech.data_snapshot import DataSnapshotPublisher


@dataclass(frozen=True)
class DataBuildResult:
    """一次构建的完整消费者数据与可审计来源摘要。"""

    payloads: Mapping[str, Any]
    source_files: tuple[Mapping[str, Any], ...] = ()


SnapshotBuilder = Callable[[bool], DataBuildResult]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot_from_runtime(private_stats_enabled: bool) -> DataBuildResult:
    """从已刷新且签名匹配的运行态数据构建一代完整消费者快照。"""

    from hextech.catalog.runtime_store import build_synergy_data_path, get_latest_valid_csv, load_runtime_csv
    from hextech.catalog.precomputed_cache import load_precomputed_champion_list, load_precomputed_hextech_map
    from hextech.overlay.hints import build_overlay_hint_cache

    csv_path = Path(get_latest_valid_csv() or "")
    if not csv_path.is_file():
        raise FileNotFoundError("没有可用于 DataService generation 的有效 CSV")
    dataframe = load_runtime_csv(str(csv_path))
    if dataframe.empty:
        raise ValueError("DataService generation 源 CSV 为空")
    raw_champions = load_precomputed_champion_list()
    raw_details = load_precomputed_hextech_map()
    if not raw_champions or not raw_details:
        raise ValueError("DataService 预计算缓存未就绪或与最新 CSV 不匹配")
    champions: list[dict[str, Any]] = []
    champion_id_by_name: dict[str, str] = {}
    for item in raw_champions:
        if not isinstance(item, Mapping):
            continue
        champion_id = str(
            item.get("id") or item.get("英雄ID") or item.get("英雄 ID") or item.get("champion_id") or ""
        ).strip()
        champion_name = str(item.get("name") or item.get("英雄名称") or item.get("hero_name") or "").strip()
        if not champion_id or not champion_name:
            continue
        champions.append({**dict(item), "id": champion_id, "name": champion_name})
        champion_id_by_name[champion_name] = champion_id
    if not champions:
        raise ValueError("DataService generation 未生成有效英雄")

    synergy_path = Path(build_synergy_data_path())
    if not synergy_path.is_file():
        raise FileNotFoundError("没有可用于 DataService generation 的联动快照")
    try:
        raw_synergy = json.loads(synergy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"DataService 联动快照无效：{synergy_path.name}") from exc
    if not isinstance(raw_synergy, Mapping) or not raw_synergy:
        raise ValueError("DataService 联动快照必须是非空对象")
    synergy_data = dict(raw_synergy)
    champion_hextech: dict[str, Any] = {}
    for champion in champions:
        name = champion["name"]
        detail = raw_details.get(name)
        if not isinstance(detail, Mapping):
            raise ValueError(f"DataService 英雄详情缓存缺失：{name}")
        cards = detail.get("comprehensive", []) if isinstance(detail, Mapping) else []
        normalized_augments: list[dict[str, Any]] = []
        for card in cards if isinstance(cards, list) else []:
            if not isinstance(card, Mapping):
                continue
            augment_id = card.get("id") or card.get("augment_id") or card.get("augmentId") or card.get("海克斯ID")
            if augment_id is None:
                continue
            normalized_augments.append({**dict(card), "id": str(augment_id)})
        if not normalized_augments:
            raise ValueError(f"DataService 英雄统计为空：{name}")
        synergy_entry = synergy_data.get(champion_id_by_name.get(name, "")) or synergy_data.get(name) or {}
        champion_hextech[name] = {
            **(dict(detail) if isinstance(detail, Mapping) else {}),
            "hero_id": champion_id_by_name.get(name, ""),
            "augments": normalized_augments,
            "synergy": dict(synergy_entry) if isinstance(synergy_entry, Mapping) else {},
        }
    overlay_hints = build_overlay_hint_cache(
        champion_hextech,
        include_private_stats=private_stats_enabled,
        source_tag="data-service",
        champion_id_by_name=champion_id_by_name,
    )
    hint_map = overlay_hints.get("hints", {}) if isinstance(overlay_hints, Mapping) else {}
    identities = {
        "champions": {champion["id"]: champion["name"] for champion in champions},
        "augments": {
            str(augment_id): str(hint.get("name") or "")
            for augment_id, hint in hint_map.items()
            if isinstance(hint, Mapping)
        },
        "augment_aliases": dict(overlay_hints.get("name_index", {})),
    }
    sources: list[dict[str, Any]] = [{
        "name": csv_path.name,
        "size": csv_path.stat().st_size,
        "sha256": _file_sha256(csv_path),
        "record_count": int(len(dataframe.index)),
    }]
    sources.append(
        {
            "name": synergy_path.name,
            "size": synergy_path.stat().st_size,
            "sha256": _file_sha256(synergy_path),
            "record_count": len(synergy_data),
        }
    )
    return DataBuildResult(
        {
            "champions": champions,
            "champion_hextech": champion_hextech,
            "overlay_hints": overlay_hints,
            "identities": identities,
        },
        tuple(sources),
    )


class DataServiceCore:
    """DataService 唯一写入者的最小可测试核心。"""

    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        builder: SnapshotBuilder,
        private_stats_enabled: bool,
    ) -> None:
        self.publisher = publisher
        self.builder = builder
        self._private_stats_enabled = bool(private_stats_enabled)
        self._action_lock = threading.Lock()
        self._last_result: dict[str, Any] = {"state": "starting", "generation_id": ""}

    def refresh(self) -> dict[str, Any]:
        with self._action_lock:
            self._last_result = self._refresh_locked()
            return dict(self._last_result)

    def set_private_stats(self, enabled: bool) -> dict[str, Any]:
        with self._action_lock:
            previous = self._private_stats_enabled
            self._private_stats_enabled = bool(enabled)
            result = self._refresh_locked()
            if result.get("state") != "ready":
                self._private_stats_enabled = previous
                result = {
                    **result,
                    "state": "failed",
                    "reason_code": "private_stats_update_failed",
                    "desired_private_stats_enabled": previous,
                }
            self._last_result = result
            return dict(self._last_result)

    def status(self) -> dict[str, Any]:
        result = dict(self._last_result)
        result["desired_private_stats_enabled"] = self._private_stats_enabled
        try:
            from hextech.data_snapshot import DataSnapshotClient

            snapshot = DataSnapshotClient(self.publisher.root).status()
        except Exception as exc:
            snapshot = {"state": "unavailable", "reason": str(exc)}
        result["snapshot"] = snapshot
        return result

    def _refresh_locked(self) -> dict[str, Any]:
        try:
            build = self.builder(self._private_stats_enabled)
            manifest = self.publisher.publish(
                build.payloads,
                private_stats_enabled=self._private_stats_enabled,
                source_files=build.source_files,
            )
        except Exception as exc:
            current_id = self.publisher.current_generation_id()
            return {
                "state": "degraded" if current_id else "failed",
                "generation_id": current_id,
                "reason_code": "refresh_failed_last_good_preserved" if current_id else "refresh_failed_no_snapshot",
                "error_type": exc.__class__.__name__,
            }
        return {
            "state": "ready",
            "generation_id": manifest.generation_id,
            "private_stats_enabled": manifest.private_stats_enabled,
        }


DATA_SERVICE_NONCE_HEADER = "X-Hextech-Data-Service-Nonce"


class DataServiceApplication:
    """只绑定 loopback 的 DataService 控制面。"""

    def __init__(self, *, core: DataServiceCore, parent_pid: int, nonce: str | None = None) -> None:
        self.core = core
        self.parent_pid = int(parent_pid)
        self.nonce = nonce or secrets.token_urlsafe(24)
        self.shutdown_requested = threading.Event()
        self._actions: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(maxsize=8)
        self._action_state_lock = threading.Lock()
        self._active_action: dict[str, Any] | None = None
        self._last_action: dict[str, Any] | None = None
        self._completed_actions: dict[str, dict[str, Any]] = {}
        self._queued_action_types: set[str] = set()
        threading.Thread(target=self._run_actions, name="hextech-data-actions", daemon=True).start()

    def submit_action(self, action_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """提交有界串行 action；HTTP 线程绝不等待真实抓取完成。"""

        if action_type not in {"refresh", "set_private_stats"}:
            return {"accepted": False, "reason_code": "unsupported_action"}
        with self._action_state_lock:
            active_type = str((self._active_action or {}).get("type") or "")
            if action_type == "refresh" and (active_type == "refresh" or action_type in self._queued_action_types):
                return {"accepted": False, "reason_code": "already_queued"}
            action_id = uuid.uuid4().hex
            try:
                self._actions.put_nowait((action_id, action_type, dict(payload or {})))
            except queue.Full:
                return {"accepted": False, "reason_code": "queue_full"}
            self._queued_action_types.add(action_type)
        return {"accepted": True, "action_id": action_id, "status": "queued"}

    def status(self) -> dict[str, Any]:
        status = self.core.status()
        with self._action_state_lock:
            status["active_action"] = dict(self._active_action) if self._active_action else None
            status["last_action"] = dict(self._last_action) if self._last_action else None
            status["actions"] = {key: dict(value) for key, value in self._completed_actions.items()}
            status["queued_action_count"] = self._actions.qsize()
        return status

    def _run_actions(self) -> None:
        while not self.shutdown_requested.is_set():
            try:
                action_id, action_type, payload = self._actions.get(timeout=0.2)
            except queue.Empty:
                continue
            started_at = time.time()
            with self._action_state_lock:
                self._queued_action_types.discard(action_type)
                self._active_action = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": "running",
                    "started_at": started_at,
                }
            try:
                if action_type == "refresh":
                    result = self.core.refresh()
                else:
                    result = self.core.set_private_stats(bool(payload.get("enabled")))
                final_status = "completed" if result.get("state") in {"ready", "degraded"} else "failed"
                completed = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": final_status,
                    "started_at": started_at,
                    "completed_at": time.time(),
                    "result": result,
                }
            except Exception as exc:
                completed = {
                    "action_id": action_id,
                    "type": action_type,
                    "status": "failed",
                    "started_at": started_at,
                    "completed_at": time.time(),
                    "result": {"state": "failed", "error_type": exc.__class__.__name__},
                }
            finally:
                with self._action_state_lock:
                    self._active_action = None
                    self._last_action = completed
                    self._completed_actions[action_id] = completed
                    while len(self._completed_actions) > 16:
                        self._completed_actions.pop(next(iter(self._completed_actions)))
                self._actions.task_done()

    def handler(self):
        application = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _authorized(self) -> bool:
                host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
                return host in {"127.0.0.1", "localhost", "::1"} and self.headers.get(DATA_SERVICE_NONCE_HEADER) == application.nonce

            def _body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except (ValueError, json.JSONDecodeError):
                    return {}
                return payload if isinstance(payload, dict) else {}

            def _send(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                elif self.path == "/v1/status":
                    self._send(HTTPStatus.OK, application.status())
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                if self.path == "/v1/actions/refresh":
                    result = application.submit_action("refresh")
                    self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
                elif self.path == "/v1/actions/set-private-stats":
                    result = application.submit_action("set_private_stats", self._body())
                    self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
                elif self.path == "/v1/shutdown":
                    application.shutdown_requested.set()
                    self._send(HTTPStatus.OK, {"state": "shutting_down"})
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        return Handler


def _refresh_and_build(private_stats_enabled: bool, *, force_refresh: bool = False) -> DataBuildResult:
    from hextech.core.refresh import refresh_backend_data

    result = refresh_backend_data(force=force_refresh)
    if result.state != "ready":
        raise RuntimeError(f"远端刷新失败：{result.reason_code}")
    return build_snapshot_from_runtime(private_stats_enabled)


class DataServiceInstanceLock:
    """持有进程级文件锁，避免多个桌面实例同时发布 generation。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_obj = self.path.open("a+b")
        try:
            file_obj.seek(0, os.SEEK_END)
            if file_obj.tell() == 0:
                file_obj.write(b"0")
                file_obj.flush()
            file_obj.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            file_obj.seek(0)
            file_obj.truncate()
            file_obj.write(json.dumps({"pid": os.getpid()}).encode("ascii"))
            file_obj.flush()
        except (OSError, BlockingIOError):
            file_obj.close()
            return False
        self._file = file_obj
        return True

    def release(self) -> None:
        file_obj = self._file
        self._file = None
        if file_obj is None:
            return
        try:
            file_obj.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        finally:
            file_obj.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hextech DataService")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--force-initial-refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from hextech.core.settings import load_ui_feature_flags

    private_enabled = bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))
    publisher = DataSnapshotPublisher()
    instance_lock = DataServiceInstanceLock(publisher.root / "data-service.lock")
    if not instance_lock.acquire():
        print("DataService 已由另一个桌面实例持有。", file=os.sys.stderr, flush=True)
        return 3
    core = DataServiceCore(
        publisher=publisher,
        builder=lambda enabled: _refresh_and_build(enabled, force_refresh=args.force_initial_refresh),
        private_stats_enabled=private_enabled,
    )
    application = DataServiceApplication(core=core, parent_pid=args.parent_pid)
    server = ThreadingHTTPServer(("127.0.0.1", 0), application.handler())
    threading.Thread(target=server.serve_forever, name="hextech-data-service-http", daemon=True).start()
    print(
        json.dumps(
            {"port": int(server.server_address[1]), "session_nonce": application.nonce, "pid": os.getpid()},
            ensure_ascii=True,
        ),
        flush=True,
    )
    skip_auto_refresh = os.getenv("HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not skip_auto_refresh:
        application.submit_action("refresh")
    next_refresh_at = time.monotonic() + 4 * 60 * 60
    try:
        while not application.shutdown_requested.wait(0.5):
            if args.parent_pid and not psutil.pid_exists(args.parent_pid):
                break
            if time.monotonic() >= next_refresh_at:
                application.submit_action("refresh")
                next_refresh_at = time.monotonic() + 4 * 60 * 60
    finally:
        server.shutdown()
        server.server_close()
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
