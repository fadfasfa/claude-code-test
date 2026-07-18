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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path

import psutil

from hextech.bootstrap.data_service_lock import DataServiceInstanceLock
from hextech.bootstrap.data_service_status import sync_startup_snapshot_status as _sync_startup_snapshot_status
from hextech.contracts import ArtifactDescriptor, SourceProvenance
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json


@dataclass(frozen=True)
class DataBuildResult:
    """一次构建的完整消费者数据与可审计来源摘要。"""

    payloads: Mapping[str, Any]
    source_files: tuple[SourceProvenance, ...] = ()


SnapshotBuilder = Callable[[], DataBuildResult]
SeedPreparer = Callable[[], bool]
RefreshAction = Callable[[bool], Mapping[str, Any]]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_payloads_from_dataframe(dataframe) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """从已清洗 CSV 直接构造查询 DTO，避免冷启动重建大型兼容缓存。"""

    import pandas as pd
    from hextech.modules.data.catalog.view_adapter import process_champions_data

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

    id_by_name: dict[str, str] = {}
    for hero_name, group in dataframe.groupby("英雄名称", sort=False):
        name = str(hero_name or "").strip()
        if name:
            id_by_name[name] = str(int(float(group.iloc[0][id_column])))
    champions = process_champions_data(dataframe, use_runtime_cache=False, log_columns=False)
    for champion in champions:
        name = str(champion.get("英雄名称") or "").strip()
        champion_id = str(champion.get("英雄 ID") or id_by_name.get(name, "")).strip()
        champion.update({"英雄 ID": champion_id, "id": champion_id, "name": name})

    details: dict[str, dict[str, Any]] = {}
    for hero_name, group in dataframe.groupby("英雄名称", sort=False):
        name = str(hero_name or "").strip()
        if not name:
            continue
        first = group.iloc[0]
        champion_id = str(int(float(first[id_column])))
        cards: list[dict[str, Any]] = []
        for raw in group.to_dict(orient="records"):
            card = {str(key): clean_value(value) for key, value in raw.items()}
            augment_id = str(int(float(card["海克斯ID"])))
            card.update({"id": augment_id, "hero_id": champion_id, "hero_name": name})
            cards.append(card)
        if cards:
            details[name] = {"hero_id": champion_id, "comprehensive": cards}
    if not champions or any(not item.get("英雄 ID") for item in champions):
        raise ValueError("DataService 冷启动英雄 DTO 构建不完整")
    return champions, details


def _build_augment_identity_payload(
    overlay_hints: Mapping[str, Any],
    catalog_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 Vision stable ID 与源站数字统计 ID 收口到同一身份索引。"""

    from hextech.modules.recommendation.hints import normalize_augment_id, normalize_augment_name

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


def _source_provenance(source: str, pointer: Mapping[str, Any]) -> SourceProvenance:
    descriptor = ArtifactDescriptor.from_mapping(pointer["artifact"])
    return SourceProvenance(
        source=source,  # type: ignore[arg-type]
        run_id=str(pointer["run_id"]),
        catalog_generation_id=str(pointer["catalog_generation_id"]),
        artifact_role=descriptor.role,
        artifact_sha256=descriptor.sha256,
        record_count=descriptor.record_count,
        manifest_sha256=str(pointer["manifest_sha256"]),
        content_schema_version=descriptor.content_schema_version,
    )


def build_snapshot_from_runtime() -> DataBuildResult:
    """从已刷新且签名匹配的运行态数据构建一代完整消费者快照。"""

    from hextech.modules.data.catalog.runtime_store import build_synergy_data_path, get_latest_valid_csv, load_runtime_csv
    from hextech.modules.data.catalog.precomputed_cache import load_precomputed_champion_list, load_precomputed_hextech_map
    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries
    from hextech.modules.recommendation.hints import build_overlay_hint_cache, enrich_overlay_hint_cache_with_catalog

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
    from hextech.modules.data.catalog.versioned import load_active_catalog
    from hextech.modules.data.source_runs import load_source_current, resolve_current_artifact

    catalog = load_active_catalog()
    pointers = {source: load_source_current(source, verify_hash=True) for source in ("hextech", "apex", "mayhem")}
    missing = [source for source, pointer in pointers.items() if not pointer]
    if missing:
        raise FileNotFoundError(f"generation 来源 current 缺失或无效：{', '.join(missing)}")
    mismatched = [
        source
        for source, pointer in pointers.items()
        if str(pointer.get("catalog_generation_id") or "") != catalog.generation_id
        or str(pointer.get("catalog_sha256") or "") != catalog.content_sha256
    ]
    if mismatched:
        raise ValueError(f"generation 来源绑定了不同 Catalog：{', '.join(mismatched)}")

    mayhem_path = resolve_current_artifact("mayhem")
    if mayhem_path:
        from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

        merge_summary = merge_mayhem_combos(
            apex_path=synergy_path,
            mayhem_raw_path=mayhem_path,
            write_output=False,
        )
        merged = merge_summary.get("merged_payload")
        if not isinstance(merged, Mapping) or not merged:
            raise ValueError("DataService Mayhem dry-run 合并结果无效")
        raw_synergy = merged
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
        include_private_stats=True,
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
    sources = [*catalog.provenance()]
    sources.extend(_source_provenance(source, pointers[source]) for source in ("hextech", "apex", "mayhem"))
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
    """无运行态 generation 时复制并验证完整 seed generation。"""

    from hextech.modules.data.ports.paths import STARTUP_SEED_DIR
    from hextech.modules.data.generation import DataSnapshotClient, default_snapshot_root

    seed_root = Path(STARTUP_SEED_DIR)
    seed_view = DataSnapshotClient(seed_root).open_view()
    runtime_root = default_snapshot_root()
    runtime_client = DataSnapshotClient(runtime_root)
    if runtime_client.status().get("state") in {"ready", "degraded"}:
        return True

    pointer = json.loads((seed_root / "current.v2.json").read_text(encoding="utf-8"))
    generation_id = str(pointer.get("current_generation_id") or "")
    if seed_view.manifest.generation_id != generation_id:
        raise ValueError("seed current 与已验证 generation 不一致")

    generations_root = runtime_root / "generations"
    generations_root.mkdir(parents=True, exist_ok=True)
    source = seed_root / "generations" / generation_id
    target = generations_root / generation_id
    if not target.exists():
        temporary = runtime_root / "staging" / f"seed-{generation_id}-{uuid.uuid4().hex[:8]}"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, temporary)
        temporary.replace(target)
    atomic_write_json(runtime_root / "current.v2.json", pointer, indent=2)
    DataSnapshotClient(runtime_root).open_view()
    return True


def bootstrap_snapshot(
    publisher: DataSnapshotPublisher,
    *,
    builder: SnapshotBuilder = build_snapshot_from_runtime,
    seed_preparer: SeedPreparer = prepare_startup_data_seed,
) -> dict[str, Any]:
    """在启动远端刷新前保证存在一代完整可读快照。"""

    from hextech.modules.data.generation import DataSnapshotClient

    client = DataSnapshotClient(publisher.root)
    current = client.status()
    if current.get("state") in {"ready", "degraded"}:
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
        seeded = client.status()
        if seeded.get("state") in {"ready", "degraded"}:
            return {
                "state": "ready",
                "generation_id": str(seeded.get("generation_id") or ""),
                "source": "verified_seed",
            }
        build = builder()
        manifest = publisher.publish(
            build.payloads,
            source_files=build.source_files,
            require_complete_provenance=True,
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
        "source": "startup_data_built",
    }


class DataServiceCore:
    """DataService 唯一写入者的最小可测试核心。"""

    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        private_stats_enabled: bool,
        refresh_action: RefreshAction,
        initial_result: Mapping[str, Any] | None = None,
    ) -> None:
        self.publisher = publisher
        self._private_stats_enabled = bool(private_stats_enabled)
        self._refresh_action = refresh_action
        self._action_lock = threading.Lock()
        self._last_result: dict[str, Any] = dict(initial_result or {"state": "starting", "generation_id": ""})
        _sync_startup_snapshot_status(self.publisher, self._last_result)

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        with self._action_lock:
            self._last_result = self._refresh_locked(force=force)
            _sync_startup_snapshot_status(self.publisher, self._last_result)
            return dict(self._last_result)

    def set_private_stats(self, enabled: bool) -> dict[str, Any]:
        with self._action_lock:
            self._private_stats_enabled = bool(enabled)
            self._last_result = {
                **self._last_result,
                "state": "ready" if self.publisher.current_generation_id() else self._last_result.get("state", "failed"),
                "generation_id": self.publisher.current_generation_id(),
                "desired_private_stats_enabled": self._private_stats_enabled,
                "reason_code": "display_policy_updated",
            }
            _sync_startup_snapshot_status(self.publisher, self._last_result)
            return dict(self._last_result)

    def status(self) -> dict[str, Any]:
        result = dict(self._last_result)
        result["desired_private_stats_enabled"] = self._private_stats_enabled
        try:
            from hextech.modules.data.generation import DataSnapshotClient

            snapshot = DataSnapshotClient(self.publisher.root).status()
        except Exception as exc:
            snapshot = {"state": "unavailable", "reason": str(exc)}
        result["snapshot"] = snapshot
        return result

    def _refresh_locked(self, *, force: bool = False) -> dict[str, Any]:
        try:
            return dict(self._refresh_action(bool(force)))
        except Exception as exc:
            current_id = self.publisher.current_generation_id()
            return {
                "state": "degraded" if current_id else "failed",
                "generation_id": current_id,
                "source": "last_good_fallback" if current_id else "remote_refresh",
                "reason_code": "refresh_failed_last_good_preserved" if current_id else "refresh_failed_no_snapshot",
                "error_type": exc.__class__.__name__,
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
        self._pending_refresh_recheck = False
        self._pending_refresh_force = False
        threading.Thread(target=self._run_actions, name="hextech-data-actions", daemon=True).start()

    def submit_action(self, action_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """提交有界串行 action；HTTP 线程绝不等待真实抓取完成。"""

        if action_type not in {"refresh", "set_private_stats"}:
            return {"accepted": False, "reason_code": "unsupported_action"}
        with self._action_state_lock:
            if self.shutdown_requested.is_set():
                return {"accepted": False, "reason_code": "shutdown_requested"}
            active_type = str((self._active_action or {}).get("type") or "")
            if action_type == "refresh" and (active_type == "refresh" or action_type in self._queued_action_types):
                self._pending_refresh_recheck = True
                self._pending_refresh_force = self._pending_refresh_force or bool((payload or {}).get("force"))
                return {
                    "accepted": True,
                    "reason_code": "pending_recheck",
                    "status": "coalesced",
                    "force": self._pending_refresh_force,
                }
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
            status["pending_refresh_recheck"] = self._pending_refresh_recheck
            status["pending_refresh_force"] = self._pending_refresh_force
        return status

    def request_shutdown(self) -> None:
        """停止接收 action；pending 只代表本进程后续工作，退出时必须丢弃。"""

        self.shutdown_requested.set()
        with self._action_state_lock:
            self._pending_refresh_recheck = False
            self._pending_refresh_force = False

    def _queue_pending_refresh_locked(self) -> None:
        if self.shutdown_requested.is_set():
            self._pending_refresh_recheck = False
            self._pending_refresh_force = False
            return
        if not self._pending_refresh_recheck or "refresh" in self._queued_action_types:
            return
        action_id = uuid.uuid4().hex
        force = self._pending_refresh_force
        try:
            self._actions.put_nowait((action_id, "refresh", {"force": force, "recheck": True}))
        except queue.Full:
            return
        self._pending_refresh_recheck = False
        self._pending_refresh_force = False
        self._queued_action_types.add("refresh")

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
                    result = self.core.refresh(force=bool(payload.get("force")))
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
                    self._queue_pending_refresh_locked()
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
                    result = application.submit_action("refresh", self._body())
                    self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
                elif self.path == "/v1/actions/set-private-stats":
                    result = application.submit_action("set_private_stats", self._body())
                    self._send(HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.CONFLICT, result)
                elif self.path == "/v1/shutdown":
                    application.request_shutdown()
                    self._send(HTTPStatus.OK, {"state": "shutting_down"})
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hextech DataService")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--force-initial-refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from hextech.modules.session.settings import load_ui_feature_flags
    from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
    from hextech.modules.data.ports.paths import get_var_dir

    private_enabled = bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))
    publisher = DataSnapshotPublisher()
    instance_lock = DataServiceInstanceLock(get_var_dir() / "locks" / "data-service.lock")
    if not instance_lock.acquire():
        print("DataService 已由另一个桌面实例持有。", file=sys.stderr, flush=True)
        return 3
    bootstrap_result = bootstrap_snapshot(publisher)
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=build_snapshot_from_runtime,
        root=get_var_dir(),
    )
    core = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=private_enabled,
        refresh_action=lambda force: coordinator.refresh(force=force),
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
        application.submit_action("refresh", {"force": args.force_initial_refresh})
    next_refresh_at = time.monotonic() + 15 * 60
    try:
        while not application.shutdown_requested.wait(0.5):
            if args.parent_pid and not psutil.pid_exists(args.parent_pid):
                break
            if time.monotonic() >= next_refresh_at:
                application.submit_action("refresh")
                next_refresh_at = time.monotonic() + 15 * 60
    finally:
        application.request_shutdown()
        coordinator.request_stop()
        server.shutdown()
        server.server_close()
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
