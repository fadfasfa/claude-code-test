"""独立 DataService 的串行 action 内核。

本模块先提供不依赖 UI/Web/Overlay 的业务内核；进程控制面只负责调用这些 action。
refresh 与私用统计切换共用一把锁，避免旧 refresh 在稍后覆盖新策略。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import secrets
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from pathlib import Path

import psutil

from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
from hextech.infrastructure.transport.loopback_http import LoopbackThreadingHTTPServer
from hextech.bootstrap.data_service_status import (
    sync_startup_service_state,
    sync_startup_snapshot_status as _sync_startup_snapshot_status,
)
from hextech.contracts import SourceProvenance
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.bootstrap.snapshot_contributions import (
    open_baseline_view as _open_baseline_view,
    validated_source_artifact as _validated_source_artifact,
)
@dataclass(frozen=True)
class DataBuildResult:
    """一次构建的完整消费者数据与可审计来源摘要。"""

    payloads: Mapping[str, Any]
    source_files: tuple[SourceProvenance, ...] = ()
SnapshotBuilder = Callable[[], DataBuildResult]
SeedPreparer = Callable[[], bool]
RefreshAction = Callable[[bool], Mapping[str, Any]]
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
        # 数字 ID 永远无歧义；名称只有唯一 canonical 候选时才可成为 alias。
        # 旧逻辑用 setdefault 让同名项按遍历顺序 first-wins，会静默绑定错误统计。
        for alias in (canonical_id,):
            if alias:
                aliases.setdefault(alias, canonical_id)
    for normalized_name, candidates in canonical_ids_by_name.items():
        if len(candidates) != 1:
            continue
        canonical_id = next(iter(candidates))
        name = augments.get(canonical_id, "")
        for alias in (name, normalize_augment_id(name), normalized_name):
            if alias:
                aliases[alias] = canonical_id

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
    from hextech.contracts import BaselineContributionV2

    if pointer.get("kind") == "baseline_generation":
        baseline = BaselineContributionV2.from_mapping(pointer)
        if baseline.source != source:
            raise ValueError(f"baseline contribution 来源不匹配：expected={source} actual={baseline.source}")
        return baseline.provenance
    artifact = pointer.get("artifact") if isinstance(pointer.get("artifact"), Mapping) else {}
    return SourceProvenance(
        source=source,  # type: ignore[arg-type]
        run_id=str(pointer["run_id"]),
        catalog_generation_id=str(pointer["catalog_generation_id"]),
        artifact_role=str(artifact["role"]),
        artifact_sha256=str(artifact["sha256"]),
        record_count=int(artifact["record_count"]),
        manifest_sha256=str(pointer["manifest_sha256"]),
        content_schema_version=int(artifact["content_schema_version"]),
    )


def build_snapshot_from_runtime(
    contributions: Mapping[str, Mapping[str, Any]] | None = None,
) -> DataBuildResult:
    """从已刷新且签名匹配的运行态数据构建一代完整消费者快照。"""

    from hextech.modules.data.catalog.runtime_store import load_runtime_csv
    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data
    from hextech.modules.recommendation.hints import (
        build_overlay_hint_cache,
        enrich_overlay_hint_cache_with_catalog,
        enrich_overlay_hint_cache_with_synergy,
    )

    from hextech.modules.data.catalog.versioned import load_active_catalog, load_runtime_catalog_from_pointer
    from hextech.modules.data.source_runs import load_source_current

    catalog = None
    if contributions is not None and isinstance(contributions.get("catalog"), Mapping):
        catalog = load_runtime_catalog_from_pointer(contributions["catalog"])
        if catalog is None:
            raise ValueError("generation Catalog contribution 无效")
    catalog = catalog or load_active_catalog()
    pointers = (
        {source: dict(contributions[source]) for source in ("hextech", "apex", "mayhem")}
        if contributions is not None
        else {source: load_source_current(source, verify_hash=True) for source in ("hextech", "apex", "mayhem")}
    )
    missing = [source for source, pointer in pointers.items() if not pointer]
    if missing:
        raise FileNotFoundError(f"generation 来源 contribution 缺失：{', '.join(missing)}")
    mismatched = [
        source
        for source, pointer in pointers.items()
        if str(pointer.get("catalog_generation_id") or "") != catalog.generation_id
        or str(pointer.get("catalog_sha256") or "") != catalog.content_sha256
    ]
    if mismatched:
        raise ValueError(f"generation 来源绑定了不同 Catalog：{', '.join(mismatched)}")

    fallback_sources = {
        source for source, pointer in pointers.items() if pointer.get("kind") == "baseline_generation"
    }
    if "hextech" in fallback_sources:
        _, fallback_view = _open_baseline_view(pointers["hextech"])
        fallback_champions = fallback_view.get_champions()
        raw_champions = fallback_champions
        raw_details = {
            str(item.get("name") or ""): fallback_view.get_champion_detail(item.get("id") or item.get("name"))
            for item in fallback_champions
            if isinstance(item, Mapping)
        }
    else:
        csv_path = _validated_source_artifact("hextech", pointers["hextech"], expected_role="stats")
        dataframe = load_runtime_csv(str(csv_path))
        if dataframe.empty:
            raise ValueError("DataService generation 源 CSV 为空")
        raw_champions, raw_details = _query_payloads_from_dataframe(dataframe)

    catalog_champions = load_champion_core_data(catalog.root)
    if not catalog_champions:
        raise ValueError("Catalog 英雄目录为空")
    raw_champions_by_id = {
        str(
            item.get("id") or item.get("英雄ID") or item.get("英雄 ID") or item.get("champion_id") or ""
        ).strip(): item
        for item in raw_champions
        if isinstance(item, Mapping)
    }
    raw_details_by_id = {
        str(detail.get("hero_id") or "").strip(): detail
        for detail in raw_details.values()
        if isinstance(detail, Mapping) and str(detail.get("hero_id") or "").strip()
    }
    champions: list[dict[str, Any]] = []
    champion_id_by_name: dict[str, str] = {}
    normalized_details: dict[str, Mapping[str, Any]] = {}
    for raw_id, catalog_item in catalog_champions.items():
        champion_id = str(raw_id).strip()
        champion_name = str(catalog_item.get("name") or "").strip() if isinstance(catalog_item, Mapping) else ""
        if not champion_id or not champion_name:
            raise ValueError(f"Catalog 英雄身份无效：{raw_id}")
        stat_item = raw_champions_by_id.get(champion_id, {})
        detail = raw_details_by_id.get(champion_id) or raw_details.get(champion_name)
        if not isinstance(detail, Mapping):
            raise ValueError(f"DataService 英雄详情缺失：{champion_name}")
        champions.append({**dict(stat_item), "id": champion_id, "name": champion_name})
        champion_id_by_name[champion_name] = champion_id
        normalized_details[champion_name] = detail

    synergy_fallback = fallback_sources.intersection({"apex", "mayhem"})
    if synergy_fallback:
        if synergy_fallback != {"apex", "mayhem"}:
            raise ValueError("Apex/Mayhem baseline 必须来自同一完整 generation")
        apex_baseline, fallback_view = _open_baseline_view(pointers["apex"])
        mayhem_baseline, _ = _open_baseline_view(pointers["mayhem"])
        if apex_baseline.origin_generation_id != mayhem_baseline.origin_generation_id:
            raise ValueError("Apex/Mayhem baseline origin generation 不一致")
        raw_synergy = fallback_view.get_synergy_data()
    else:
        apex_path = _validated_source_artifact("apex", pointers["apex"], expected_role="synergy")
        mayhem_path = _validated_source_artifact("mayhem", pointers["mayhem"], expected_role="combos")
        try:
            raw_synergy = json.loads(apex_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"DataService Apex artifact 无效：{apex_path.name}") from exc
        from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

        catalog_files = {item.role: catalog.root / item.relative_path for item in catalog.manifest.files}
        merge_summary = merge_mayhem_combos(
            apex_path=apex_path,
            mayhem_raw_path=mayhem_path,
            augment_manifest_path=catalog_files["augments"],
            core_data_path=catalog_files["champions"],
            write_output=False,
        )
        merged = merge_summary.get("merged_payload")
        if not isinstance(merged, Mapping) or not merged:
            raise ValueError("DataService Mayhem dry-run 合并结果无效")
        raw_synergy = merged
    if not isinstance(raw_synergy, Mapping) or not raw_synergy:
        raise ValueError("DataService 联动 contribution 必须是非空对象")
    synergy_data = dict(raw_synergy)
    champion_hextech: dict[str, Any] = {}
    for champion in champions:
        name = champion["name"]
        detail = normalized_details.get(name)
        if not isinstance(detail, Mapping):
            raise ValueError(f"DataService 英雄详情缓存缺失：{name}")
        cards = (detail.get("comprehensive") or detail.get("augments") or []) if isinstance(detail, Mapping) else []
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
        synergy_by_name={},
        champion_id_by_name=champion_id_by_name,
    )
    catalog_entries = load_augment_manifest_entries(catalog.root)
    augment_identities = _build_augment_identity_payload(overlay_hints, catalog_entries)
    enrich_overlay_hint_cache_with_catalog(overlay_hints, catalog_entries)
    from hextech.modules.recommendation.synergy_projection import load_previous_synergy_projection_report

    previous_projection = load_previous_synergy_projection_report()
    enrich_overlay_hint_cache_with_synergy(
        overlay_hints,
        synergy_data,
        previous_report=previous_projection,
    )
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
            result = dict(self._refresh_action(bool(force)))
            if result.get("state") == "degraded" and self.publisher.current_generation_id():
                result.setdefault("data_status", "data_stale")
                result.setdefault("data_reason", "candidate_rejected_last_good_preserved")
            return result
        except Exception as exc:
            current_id = self.publisher.current_generation_id()
            return {
                "state": "degraded" if current_id else "failed",
                "generation_id": current_id,
                "source": "last_good_fallback" if current_id else "remote_refresh",
                "reason_code": "refresh_failed_last_good_preserved" if current_id else "refresh_failed_no_snapshot",
                "data_status": "data_stale" if current_id else "unavailable",
                "data_reason": "refresh_exception_last_good_preserved" if current_id else "no_snapshot",
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
    from hextech.infrastructure.sources.hextech.service import probe_hextech_upstream_marker

    private_enabled = bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))
    publisher = DataSnapshotPublisher()
    instance_lock = InterProcessFileLock(get_var_dir() / "locks" / "data-service.lock")
    if not instance_lock.acquire():
        logging.getLogger(__name__).error("DataService 已由另一个桌面实例持有。")
        return 3
    sync_startup_service_state(publisher, "starting")
    try:
        bootstrap_result = bootstrap_snapshot(publisher)
    except Exception as exc:
        sync_startup_service_state(publisher, "failed", error_summary=f"{exc.__class__.__name__}: {exc}")
        instance_lock.release()
        raise
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=build_snapshot_from_runtime,
        root=get_var_dir(),
        upstream_marker_probe=probe_hextech_upstream_marker,
    )
    core = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=private_enabled,
        refresh_action=lambda force: coordinator.refresh(force=force),
        initial_result=bootstrap_result,
    )
    application = DataServiceApplication(core=core, parent_pid=args.parent_pid)
    server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), application.handler())
    threading.Thread(target=server.serve_forever, name="hextech-data-service-http", daemon=True).start()
    from hextech.modules.session.process_bootstrap import publish_process_bootstrap

    publish_process_bootstrap(
        {"port": int(server.server_address[1]), "session_nonce": application.nonce, "pid": os.getpid()}
    )
    skip_auto_refresh = os.getenv("HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH", "").strip().lower() in {"1", "true", "yes", "on"}
    if not skip_auto_refresh:
        application.submit_action("refresh", {"force": args.force_initial_refresh})
    next_refresh_at = time.monotonic() + 15 * 60
    try:
        while not application.shutdown_requested.wait(0.5):
            if args.parent_pid and not psutil.pid_exists(args.parent_pid):
                break
            if (resumed_force := coordinator.poll_deferred_refresh()) is not None:
                application.submit_action("refresh", {"force": resumed_force, "resumed_after_game": True})
            if time.monotonic() >= next_refresh_at:
                application.submit_action("refresh")
                next_refresh_at = time.monotonic() + 15 * 60
    finally:
        sync_startup_service_state(publisher, "stopping")
        application.request_shutdown()
        coordinator.request_stop()
        server.shutdown()
        server.server_close()
        instance_lock.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
