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
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path

import psutil

from hextech.data_snapshot import DataSnapshotPublisher
from hextech.support.atomic_io import atomic_write_json


@dataclass(frozen=True)
class DataBuildResult:
    """一次构建的完整消费者数据与可审计来源摘要。"""

    payloads: Mapping[str, Any]
    source_files: tuple[Mapping[str, Any], ...] = ()


SnapshotBuilder = Callable[[bool], DataBuildResult]
SeedPreparer = Callable[[], bool]


def _sync_startup_snapshot_status(publisher: DataSnapshotPublisher, result: Mapping[str, Any]) -> None:
    """generation 切换后同步公共启动状态，避免 Web 与 runtime status 跨代。"""

    from hextech.data_snapshot import DataSnapshotClient

    if publisher.root.name != "snapshots":
        return
    try:
        client = DataSnapshotClient(publisher.root)
        snapshot_status = client.status()
        if snapshot_status.get("state") not in {"ready", "degraded"}:
            return
        manifest = client.load_manifest()
        status_path = publisher.root.parent / "state" / "startup_status.json"
        payload: dict[str, Any] = {}
        if status_path.is_file():
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        synergy_ready = any(
            str(item.get("name") or "") == "Champion_Synergy_Cleaned.json"
            for item in manifest.source_files
            if isinstance(item, Mapping)
        )
        payload.update(
            {
                "first_run": False,
                "hero_ready": manifest.champion_count > 0,
                "hextech_ready": manifest.stat_record_count > 0,
                "synergy_ready": synergy_ready,
                "in_progress_tasks": [],
                "last_error": "" if result.get("state") == "ready" else str(result.get("reason_code") or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "data_snapshot": {
                    **snapshot_status,
                    "source": str(result.get("source") or "runtime_current"),
                    "champion_count": manifest.champion_count,
                    "augment_count": manifest.augment_count,
                    "stat_record_count": manifest.stat_record_count,
                },
            }
        )
        atomic_write_json(status_path, payload, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # status 是诊断投影，写入失败不能回滚已经原子发布的 generation。
        return


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_payloads_from_dataframe(dataframe) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """从已清洗 CSV 直接构造查询 DTO，避免冷启动重建大型兼容缓存。"""

    import pandas as pd

    id_column = "英雄ID" if "英雄ID" in dataframe.columns else "英雄 ID"
    required = {id_column, "英雄名称", "海克斯ID", "海克斯名称"}
    if not required.issubset(set(dataframe.columns)):
        raise ValueError("DataService generation 源 CSV schema 不完整")

    def clean_value(value: Any) -> Any:
        if pd.isna(value):
            return None
        if hasattr(value, "item"):
            return value.item()
        return value

    champions: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    for hero_name, group in dataframe.groupby("英雄名称", sort=False):
        name = str(hero_name or "").strip()
        if not name:
            continue
        first = group.iloc[0]
        champion_id = str(int(float(first[id_column])))
        champion = {key: clean_value(first[key]) for key in (id_column, "英雄名称", "英雄评级", "英雄胜率", "英雄出场率") if key in group.columns}
        champion.update({"id": champion_id, "name": name})
        cards: list[dict[str, Any]] = []
        for raw in group.to_dict(orient="records"):
            card = {str(key): clean_value(value) for key, value in raw.items()}
            augment_id = str(int(float(card["海克斯ID"])))
            card.update({"id": augment_id, "hero_id": champion_id, "hero_name": name})
            cards.append(card)
        if cards:
            champions.append(champion)
            details[name] = {"hero_id": champion_id, "comprehensive": cards}
    return champions, details


def _build_augment_identity_payload(
    overlay_hints: Mapping[str, Any],
    catalog_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 Vision stable ID 与源站数字统计 ID 收口到同一身份索引。"""

    from hextech.overlay.hints import normalize_augment_id, normalize_augment_name

    hint_map = overlay_hints.get("hints", {})
    if not isinstance(hint_map, Mapping):
        hint_map = {}

    augments: dict[str, str] = {}
    canonical_ids_by_name: dict[str, set[str]] = {}
    for raw_id, raw_hint in hint_map.items():
        if not isinstance(raw_hint, Mapping):
            continue
        canonical_id = str(raw_id).strip()
        name = str(raw_hint.get("name") or "").strip()
        if not canonical_id.isdecimal() or not name:
            continue
        augments[canonical_id] = name
        canonical_ids_by_name.setdefault(normalize_augment_name(name), set()).add(canonical_id)

    aliases: dict[str, str] = {}
    for canonical_id, name in augments.items():
        for alias in (canonical_id, name, normalize_augment_id(name), normalize_augment_name(name)):
            if alias:
                aliases.setdefault(alias, canonical_id)

    catalog_augments: dict[str, dict[str, Any]] = {}
    for entry in catalog_entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        stable_id = normalize_augment_id(entry.get("augment_name_id"), name)
        if not name or not stable_id:
            continue
        candidates = canonical_ids_by_name.get(normalize_augment_name(name), set())
        canonical_id = next(iter(candidates)) if len(candidates) == 1 else ""
        item = {
            "vision_id": stable_id,
            "name": name,
            "tier": str(entry.get("tier") or "").strip(),
            "canonical_id": canonical_id,
            "stats_available": bool(canonical_id),
            "ambiguous": len(candidates) > 1,
        }
        existing = catalog_augments.get(stable_id)
        if existing and existing != item:
            existing["ambiguous"] = True
            existing["canonical_id"] = ""
            existing["stats_available"] = False
            continue
        catalog_augments[stable_id] = item
        if canonical_id:
            for alias in (
                stable_id,
                str(entry.get("augment_name_id") or "").strip(),
                name,
                normalize_augment_id(name),
                normalize_augment_name(name),
            ):
                if alias:
                    aliases.setdefault(alias, canonical_id)

    return {
        "schema_version": 2,
        "augments": augments,
        "augment_aliases": aliases,
        "catalog_augments": catalog_augments,
    }


def build_snapshot_from_runtime(private_stats_enabled: bool) -> DataBuildResult:
    """从已刷新且签名匹配的运行态数据构建一代完整消费者快照。"""

    from hextech.catalog.runtime_store import build_synergy_data_path, get_latest_valid_csv, load_runtime_csv
    from hextech.catalog.precomputed_cache import load_precomputed_champion_list, load_precomputed_hextech_map
    from hextech.catalog.version_catalog import load_augment_manifest_entries
    from hextech.overlay.hints import build_overlay_hint_cache, enrich_overlay_hint_cache_with_catalog

    csv_path = Path(get_latest_valid_csv() or "")
    if not csv_path.is_file():
        raise FileNotFoundError("没有可用于 DataService generation 的有效 CSV")
    dataframe = load_runtime_csv(str(csv_path))
    if dataframe.empty:
        raise ValueError("DataService generation 源 CSV 为空")
    raw_champions = load_precomputed_champion_list()
    raw_details = load_precomputed_hextech_map()
    if not raw_champions or not raw_details:
        raw_champions, raw_details = _query_payloads_from_dataframe(dataframe)
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
    catalog_entries = load_augment_manifest_entries()
    augment_identities = _build_augment_identity_payload(overlay_hints, catalog_entries)
    enrich_overlay_hint_cache_with_catalog(overlay_hints, catalog_entries)
    identities = {
        "champions": {champion["id"]: champion["name"] for champion in champions},
        **augment_identities,
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


def prepare_startup_data_seed() -> bool:
    """源码态无 generation 时，把只读 startup 数据播种到可写 runtime。

    打包态通常已由 bundle manifest 播种完整 generation；这里主要保证源码 UI
    不必把首次联网刷新当作进入游戏前的硬依赖。仅补缺失文件，不覆盖运行数据。
    """

    from hextech.catalog.runtime_store import get_runtime_hextech_data_dir, get_runtime_synergy_data_dir
    from hextech.scraping._paths import BUNDLE_ROOT_DIR

    seed_root = Path(BUNDLE_ROOT_DIR) / "data" / "seed" / "startup"
    seed_hextech = seed_root / "hextech"
    seed_synergy = seed_root / "synergy"
    runtime_hextech = Path(get_runtime_hextech_data_dir())
    runtime_synergy = Path(get_runtime_synergy_data_dir())
    csv_sources = sorted(seed_hextech.glob("Hextech_Data_*.csv"), key=lambda path: path.name, reverse=True)
    synergy_sources = sorted(seed_synergy.glob("Champion_Synergy_*.json"), key=lambda path: path.name)
    if not csv_sources or not synergy_sources:
        return False

    runtime_hextech.mkdir(parents=True, exist_ok=True)
    runtime_synergy.mkdir(parents=True, exist_ok=True)
    copied = False
    for source, target_dir in ((csv_sources[0], runtime_hextech), *[(path, runtime_synergy) for path in synergy_sources]):
        target = target_dir / source.name
        if target.exists():
            continue
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            copied = True
        finally:
            temporary.unlink(missing_ok=True)

    return copied or bool((runtime_hextech / csv_sources[0].name).is_file() and any(runtime_synergy.glob("Champion_Synergy_*.json")))


def bootstrap_snapshot(
    publisher: DataSnapshotPublisher,
    private_stats_enabled: bool,
    *,
    builder: SnapshotBuilder = build_snapshot_from_runtime,
    seed_preparer: SeedPreparer = prepare_startup_data_seed,
) -> dict[str, Any]:
    """在启动远端刷新前保证存在一代完整可读快照。"""

    from hextech.data_snapshot import DataSnapshotClient

    current = DataSnapshotClient(publisher.root).status()
    if current.get("state") in {"ready", "degraded"}:
        try:
            identity_indexes = DataSnapshotClient(publisher.root).get_identity_indexes()
            identity_contract_ready = int(identity_indexes.get("schema_version") or 0) >= 2
        except (TypeError, ValueError):
            identity_contract_ready = False
        if not identity_contract_ready:
            try:
                seed_preparer()
                build = builder(bool(private_stats_enabled))
                manifest = publisher.publish(
                    build.payloads,
                    private_stats_enabled=bool(private_stats_enabled),
                    source_files=build.source_files,
                )
                return {
                    "state": "ready",
                    "generation_id": manifest.generation_id,
                    "private_stats_enabled": manifest.private_stats_enabled,
                    "source": "startup_data_built",
                    "reason_code": "identity_schema_upgraded",
                }
            except Exception as exc:
                # 旧代仍可作为 last-good，但必须暴露身份契约尚未升级，不能静默冒充 ready。
                return {
                    "state": "degraded",
                    "generation_id": str(current.get("generation_id") or ""),
                    "source": "last_good_fallback",
                    "reason_code": "identity_schema_upgrade_failed",
                    "error_type": exc.__class__.__name__,
                }
        source = "last_good_fallback" if current.get("state") == "degraded" else "runtime_current"
        if source == "runtime_current":
            try:
                startup_status = json.loads((publisher.root.parent / "state" / "startup_status.json").read_text(encoding="utf-8"))
                seeded = startup_status.get("data_snapshot") if isinstance(startup_status, Mapping) else None
                if (
                    isinstance(seeded, Mapping)
                    and seeded.get("source") == "verified_bundle_seed"
                    and str(seeded.get("generation_id") or "") == str(current.get("generation_id") or "")
                ):
                    source = "verified_seed"
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return {
            "state": str(current["state"]),
            "generation_id": str(current.get("generation_id") or ""),
            "source": source,
        }
    try:
        if not seed_preparer():
            raise FileNotFoundError("没有可用的 startup 数据 seed")
        build = builder(bool(private_stats_enabled))
        manifest = publisher.publish(
            build.payloads,
            private_stats_enabled=bool(private_stats_enabled),
            source_files=build.source_files,
        )
    except Exception as exc:
        return {
            "state": "failed",
            "generation_id": "",
            "source": "startup_data_built",
            "reason_code": "bootstrap_failed_no_snapshot",
            "error_type": exc.__class__.__name__,
        }
    return {
        "state": "ready",
        "generation_id": manifest.generation_id,
        "private_stats_enabled": manifest.private_stats_enabled,
        "source": "startup_data_built",
    }


class DataServiceCore:
    """DataService 唯一写入者的最小可测试核心。"""

    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        builder: SnapshotBuilder,
        private_stats_enabled: bool,
        initial_result: Mapping[str, Any] | None = None,
    ) -> None:
        self.publisher = publisher
        self.builder = builder
        self._private_stats_enabled = bool(private_stats_enabled)
        self._action_lock = threading.Lock()
        self._last_result: dict[str, Any] = dict(initial_result or {"state": "starting", "generation_id": ""})
        _sync_startup_snapshot_status(self.publisher, self._last_result)

    def refresh(self) -> dict[str, Any]:
        with self._action_lock:
            self._last_result = self._refresh_locked()
            _sync_startup_snapshot_status(self.publisher, self._last_result)
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
            _sync_startup_snapshot_status(self.publisher, self._last_result)
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
                "source": "last_good_fallback" if current_id else "remote_refresh",
                "reason_code": "refresh_failed_last_good_preserved" if current_id else "refresh_failed_no_snapshot",
                "error_type": exc.__class__.__name__,
            }
        return {
            "state": "ready",
            "generation_id": manifest.generation_id,
            "private_stats_enabled": manifest.private_stats_enabled,
            "source": "remote_refresh",
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

    result = refresh_backend_data(
        force=force_refresh,
        rebuild_compat_cache=False,
    )
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
        print("DataService 已由另一个桌面实例持有。", file=sys.stderr, flush=True)
        return 3
    bootstrap_result = bootstrap_snapshot(publisher, private_enabled)
    core = DataServiceCore(
        publisher=publisher,
        builder=lambda enabled: _refresh_and_build(enabled, force_refresh=args.force_initial_refresh),
        private_stats_enabled=private_enabled,
        initial_result=bootstrap_result,
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
