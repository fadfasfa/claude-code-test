"""独立 DataService 的串行 action 内核。

本模块先提供不依赖 UI/Web/Overlay 的业务内核；进程控制面只负责调用这些 action。
refresh 与私用统计切换共用一把锁，避免旧 refresh 在稍后覆盖新策略。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from hextech.bootstrap.aramkit_generation import build_aramkit_payloads as _aramkit_payloads
from hextech.bootstrap.blitz_generation import build_blitz_details as _blitz_details
from hextech.bootstrap.legacy_generation import query_payloads_from_dataframe as _query_payloads_from_dataframe  # noqa: F401
from hextech.bootstrap.startup_refresh import StartupRefreshSchedule, initial_auto_refresh_delay_seconds  # noqa: F401
from hextech.bootstrap.game_refresh_gate import normalize_refresh_scope
from hextech.bootstrap.data_service_application import (
    DATA_SERVICE_NONCE_HEADER as _DATA_SERVICE_NONCE_HEADER,
    DataServiceApplication,
)
DATA_SERVICE_NONCE_HEADER = _DATA_SERVICE_NONCE_HEADER
@dataclass(frozen=True)
class DataBuildResult:
    """一次构建的完整消费者数据与可审计来源摘要。"""

    payloads: Mapping[str, Any]
    source_files: tuple[SourceProvenance, ...] = ()
SnapshotBuilder = Callable[[], DataBuildResult]
SeedPreparer = Callable[[], bool]
RefreshAction = Callable[[bool, str], Mapping[str, Any]]
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


def _normalized_augment_cards(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """统一 Blitz 详情的 augment ID；缺少 ARAMKit 英雄行时 Overlay 仍可消费排名。"""

    cards = detail.get("comprehensive") or detail.get("augments") or []
    normalized: list[dict[str, Any]] = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, Mapping):
            continue
        augment_id = card.get("id") or card.get("augment_id") or card.get("augmentId") or card.get("海克斯ID")
        if augment_id is None:
            continue
        normalized.append({**dict(card), "id": str(augment_id)})
    return normalized


def build_snapshot_from_runtime(
    contributions: Mapping[str, Mapping[str, Any]] | None = None,
) -> DataBuildResult:
    """从已刷新且签名匹配的运行态数据构建一代完整消费者快照。"""

    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data
    from hextech.modules.recommendation.hints import (
        build_overlay_hint_cache,
        enrich_overlay_hint_cache_with_catalog,
        enrich_overlay_hint_cache_with_synergy,
    )

    from hextech.modules.data.catalog.versioned import load_active_catalog, load_runtime_catalog_from_pointer
    from hextech.modules.data.source_runs import load_source_current
    from hextech.bootstrap.production_pool_binding import bind_production_pool

    catalog = None
    if contributions is not None and isinstance(contributions.get("catalog"), Mapping):
        catalog = load_runtime_catalog_from_pointer(contributions["catalog"])
        if catalog is None:
            raise ValueError("generation Catalog contribution 无效")
    catalog = catalog or load_active_catalog()
    pointers = (
        {source: dict(contributions[source]) for source in ("aramkit", "blitz", "apex", "mayhem")}
        if contributions is not None
        else {source: load_source_current(source, verify_hash=True) for source in ("aramkit", "blitz", "apex", "mayhem")}
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
    if "aramkit" in fallback_sources:
        _, fallback_view = _open_baseline_view(pointers["aramkit"])
        raw_champions = fallback_view.get_champions()
    else:
        raw_champions, _ = _aramkit_payloads(pointers["aramkit"], catalog=catalog)
    if "blitz" in fallback_sources:
        _, fallback_view = _open_baseline_view(pointers["blitz"])
        raw_details = {
            str(item.get("name") or ""): fallback_view.get_champion_detail(item.get("id") or item.get("name"))
            for item in raw_champions
            if isinstance(item, Mapping)
        }
    else:
        raw_details = _blitz_details(pointers["blitz"], catalog=catalog)

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
    for raw_item in raw_champions:
        if not isinstance(raw_item, Mapping):
            continue
        champion_id = str(
            raw_item.get("id")
            or raw_item.get("英雄ID")
            or raw_item.get("英雄 ID")
            or raw_item.get("champion_id")
            or ""
        ).strip()
        catalog_item = catalog_champions.get(champion_id)
        champion_name = str(catalog_item.get("name") or "").strip() if isinstance(catalog_item, Mapping) else ""
        if not champion_id or not champion_name:
            raise ValueError(f"来源英雄无法绑定 Catalog：{champion_id}")
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
        normalized_augments = _normalized_augment_cards(detail)
        if not normalized_augments:
            raise ValueError(f"DataService 英雄统计为空：{name}")
        synergy_entry = synergy_data.get(champion_id_by_name.get(name, "")) or synergy_data.get(name) or {}
        champion_hextech[name] = {
            **(dict(detail) if isinstance(detail, Mapping) else {}),
            "hero_id": champion_id_by_name.get(name, ""),
            "augments": normalized_augments,
            "synergy": dict(synergy_entry) if isinstance(synergy_entry, Mapping) else {},
        }

    # ARAMKit 只负责英雄总体榜，缺少单个英雄不能抹掉 Blitz 已提供的 Overlay 排名。
    # Web 榜单仍只发布真实 ARAMKit 行；额外英雄仅参与 overlay_hints 构建。
    overlay_hextech = dict(champion_hextech)
    overlay_champion_ids = dict(champion_id_by_name)
    for champion_id, catalog_item in catalog_champions.items():
        champion_name = str(catalog_item.get("name") or "").strip()
        if not champion_name or champion_name in overlay_hextech:
            continue
        detail = raw_details_by_id.get(str(champion_id)) or raw_details.get(champion_name)
        if not isinstance(detail, Mapping):
            continue
        normalized_augments = _normalized_augment_cards(detail)
        if not normalized_augments:
            continue
        overlay_hextech[champion_name] = {
            **dict(detail),
            "hero_id": str(champion_id),
            "augments": normalized_augments,
            "synergy": {},
        }
        overlay_champion_ids[champion_name] = str(champion_id)
    overlay_hints = build_overlay_hint_cache(
        overlay_hextech,
        include_private_stats=True,
        source_tag="data-service",
        synergy_by_name={},
        champion_id_by_name=overlay_champion_ids,
    )
    catalog_entries = load_augment_manifest_entries(catalog.root)
    augment_identities = _build_augment_identity_payload(overlay_hints, catalog_entries)
    enrich_overlay_hint_cache_with_catalog(overlay_hints, catalog_entries)
    bind_production_pool(
        overlay_hints,
        catalog=catalog,
        stats_pointer=pointers["blitz"],
        legacy_baseline="blitz" in fallback_sources,
    )
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
    sources.extend(_source_provenance(source, pointers[source]) for source in ("aramkit", "blitz", "apex", "mayhem"))
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

    def refresh(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        with self._action_lock:
            self._last_result = self._refresh_locked(force=force, scope=scope)
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

    def _refresh_locked(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        normalized_scope = normalize_refresh_scope(scope)
        try:
            result = dict(self._refresh_action(bool(force), normalized_scope))
            result.setdefault("refresh_scope", normalized_scope)
            result.setdefault("force", bool(force))
            if result.get("state") == "degraded" and self.publisher.current_generation_id():
                result.setdefault(
                    "data_status",
                    "fresh" if result.get("reason_code") == "optional_source_stale" else "data_stale",
                )
                result.setdefault(
                    "data_reason",
                    "optional_source_stale"
                    if result.get("reason_code") == "optional_source_stale"
                    else "candidate_rejected_last_good_preserved",
                )
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
                "refresh_scope": normalized_scope,
                "force": bool(force),
            }
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hextech DataService")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--force-initial-refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from hextech.modules.session.settings import load_ui_feature_flags
    from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
    from hextech.modules.data.ports.paths import get_var_dir
    from hextech.infrastructure.sources.aramkit.service import probe_aramkit_upstream_marker
    from hextech.bootstrap.game_refresh_gate import probe_production_game_in_progress

    private_enabled = bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))
    publisher = DataSnapshotPublisher()
    instance_lock = InterProcessFileLock(get_var_dir() / "locks" / "data-service.lock")
    if not instance_lock.acquire():
        logging.getLogger(__name__).error("DataService 已由另一个桌面实例持有。")
        return 3
    from hextech.modules.session.runtime_role_owner import publish_role_owner, remove_role_owner

    publish_role_owner("data-service")
    sync_startup_service_state(publisher, "starting")
    try:
        bootstrap_result = bootstrap_snapshot(publisher)
    except Exception as exc:
        sync_startup_service_state(publisher, "failed", error_summary=f"{exc.__class__.__name__}: {exc}")
        remove_role_owner("data-service")
        instance_lock.release()
        raise
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=build_snapshot_from_runtime,
        root=get_var_dir(),
        upstream_marker_probe=probe_aramkit_upstream_marker,
        game_state_probe=probe_production_game_in_progress,
    )
    core = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=private_enabled,
        refresh_action=lambda force, scope: coordinator.refresh(force=force, scope=scope),
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
    initial_refresh = StartupRefreshSchedule.create(bootstrap_result, skip=skip_auto_refresh, now=time.monotonic())
    if initial_refresh.consume_if_due(time.monotonic()):
        application.submit_action("refresh", {"force": args.force_initial_refresh})
    next_refresh_at = time.monotonic() + 15 * 60
    try:
        while not application.shutdown_requested.wait(0.5):
            if args.parent_pid and not psutil.pid_exists(args.parent_pid):
                break
            if initial_refresh.consume_if_due(time.monotonic()):
                payload = {"force": args.force_initial_refresh, "startup_grace_seconds": initial_refresh.delay_seconds}
                application.submit_action("refresh", payload)
            if (resume_request := coordinator.poll_deferred_refresh()) is not None:
                application.submit_action(
                    "refresh",
                    {**resume_request, "resumed_after_game": True},
                )
            if time.monotonic() >= next_refresh_at:
                application.submit_action("refresh")
                next_refresh_at = time.monotonic() + 15 * 60
    finally:
        sync_startup_service_state(publisher, "stopping")
        application.request_shutdown()
        coordinator.request_stop()
        server.shutdown()
        server.server_close()
        remove_role_owner("data-service")
        instance_lock.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
