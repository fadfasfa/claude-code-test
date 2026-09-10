"""只读验证 v2 Catalog、来源 cohort、generation 与消费者追溯关系。

业务门禁由 ``modules`` 下的纯 validation 函数维护；本工具只定位正式 artifact、
固定 generation 视图并输出统一报告，不触发抓取、promotion 或 seed 提升。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.contracts import SourcePointerV2, SourceProvenance, SourceRunManifestV2
from hextech.modules.acquisition.apex.validation import (
    validate_apex_removal_evidence,
    validate_apex_run,
)
from hextech.infrastructure.sources.aramkit.service import validate_scoped_stats_artifact
from hextech.infrastructure.sources.blitz.service import validate_blitz_artifact
from hextech.modules.acquisition.mayhem.validation import (
    validate_mayhem_removal_evidence,
    validate_mayhem_run,
)
from hextech.modules.data.catalog.version_catalog import load_champion_core_data
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.refresh_checkpoint import CatalogAdoptionCheckpointStore
from hextech.modules.data.catalog.versioned import load_runtime_catalog_from_pointer, sha256_file
from hextech.modules.data.generation import (
    DataSnapshotClient,
    SnapshotValidationError,
    default_snapshot_root,
)
from hextech.modules.data.generation.validation import content_fingerprint, validate_complete_provenance
from hextech.modules.data.freshness import SOURCE_INTERVALS, STALE_AGE_FACTOR
from hextech.modules.data.source_runs import (
    load_source_current,
    source_run_artifact_path,
    source_run_dir,
)


ACTIVE_SOURCES = ("aramkit", "blitz", "apex", "mayhem")
OPTIONAL_SOURCES = frozenset({"blitz", "apex", "mayhem"})


class AcceptanceFailure(RuntimeError):
    """验收证据缺失、过期、损坏或跨 cohort 不一致。"""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"验收 JSON 无法读取：{path}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceFailure(f"验收 JSON 必须是对象：{path}")
    return payload


def _parse_time(value: object, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceFailure(f"{field_name} 时间无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_fresh(value: object, maximum_age: timedelta, *, label: str, now: datetime) -> None:
    completed = _parse_time(value, field_name=f"{label}.last_success_at")
    if completed > now + timedelta(minutes=5) or now - completed > maximum_age:
        raise AcceptanceFailure(f"{label} 已过期：last_success_at={completed.isoformat()}")


def _blocked_adoption_evidence(catalog_pointer: Mapping[str, Any]) -> dict[str, Any]:
    adoption = CatalogAdoptionCheckpointStore().load()
    candidate = adoption.get("catalog")
    completed = adoption.get("completed_sources")
    pending = adoption.get("pending_sources")
    if (
        adoption.get("state") != "blocked"
        or not isinstance(candidate, Mapping)
        or not isinstance(completed, Mapping)
        or not isinstance(pending, list)
    ):
        return {}
    current_identity = (
        str(catalog_pointer.get("catalog_generation_id") or ""),
        str(catalog_pointer.get("content_sha256") or ""),
    )
    candidate_identity = (
        str(candidate.get("catalog_generation_id") or ""),
        str(candidate.get("content_sha256") or ""),
    )
    pending_names = {str(source) for source in pending}
    completed_names = {str(source) for source in completed}
    if (
        not all(current_identity)
        or not all(candidate_identity)
        or current_identity == candidate_identity
        or pending_names - set(ACTIVE_SOURCES)
        or pending_names & completed_names
        or load_runtime_catalog_from_pointer(candidate) is None
    ):
        return {}
    return {
        "state": "blocked",
        "catalog_generation_id": candidate_identity[0],
        "completed_sources": sorted(completed_names),
        "pending_sources": sorted(pending_names),
    }


def _validate_generation_freshness(
    status: Mapping[str, Any],
    source_report: Mapping[str, Any],
) -> None:
    state = str(status.get("state") or "")
    if state not in {"ready", "degraded"}:
        raise AcceptanceFailure("strict 验收 generation 不可用")
    degraded_sources = {
        str(source)
        for source in (
            status.get("effective_degraded_sources")
            or status.get("degraded_sources")
            or []
        )
    }
    if degraded_sources - OPTIONAL_SOURCES:
        raise AcceptanceFailure(
            "strict 验收只允许 optional 来源降级："
            + ",".join(sorted(degraded_sources - OPTIONAL_SOURCES))
        )
    source_status = status.get("source_status")
    source_status = source_status if isinstance(source_status, Mapping) else {}
    aramkit = source_status.get("aramkit")
    if (
        not isinstance(aramkit, Mapping)
        or aramkit.get("freshness") != "fresh"
        or aramkit.get("data_status") != "fresh"
    ):
        raise AcceptanceFailure("strict 验收要求 ARAMKit fresh/fresh")
    for source in degraded_sources:
        item = source_status.get(source)
        if (
            not isinstance(item, Mapping)
            or item.get("data_status") == "fresh"
        ):
            raise AcceptanceFailure(f"optional 降级来源未 fail closed：{source}")
    stale_optional = {
        source
        for source in OPTIONAL_SOURCES
        if isinstance(source_report.get(source), Mapping)
        and source_report[source].get("freshness") == "stale"
    }
    if stale_optional - degraded_sources:
        raise AcceptanceFailure(
            "过期 optional 来源未标记 degraded："
            + ",".join(sorted(stale_optional - degraded_sources))
        )


def verify_real_session_evidence(path: Path, *, expected_generation_id: str) -> dict[str, Any]:
    """验证 LCU、窗口、Vision、推荐、渲染和截图属于同一真实会话。"""

    payload = _read_object(path)
    if payload.get("evidence_kind") != "real_game_session":
        raise AcceptanceFailure("缺少真实游戏会话证据标记")
    generation_id = str(payload.get("generation_id") or "")
    session_id = str(payload.get("session_id") or "")
    if generation_id != expected_generation_id:
        raise AcceptanceFailure("真实会话 generation 与已验证快照不一致")
    if not session_id:
        raise AcceptanceFailure("真实会话缺少 session_id")
    sections: dict[str, dict[str, Any]] = {}
    for name in ("lcu", "window", "vision", "recommendation", "final_state"):
        section = payload.get(name)
        if not isinstance(section, dict):
            raise AcceptanceFailure(f"真实会话缺少 {name} 证据")
        if str(section.get("session_id") or "") != session_id:
            raise AcceptanceFailure(f"真实会话 session 不一致：{name}")
        sections[name] = section
    if not str(sections["lcu"].get("local_champion_id") or ""):
        raise AcceptanceFailure("真实 LCU 未取得本地英雄")
    window = sections["window"]
    if int(window.get("hwnd") or 0) <= 0 or not window.get("client_size") or not window.get("capture_size"):
        raise AcceptanceFailure("真实游戏窗口证据不完整")
    if float(window.get("dpi_scale") or 0) <= 0:
        raise AcceptanceFailure("真实游戏窗口 DPI 证据无效")
    vision = sections["vision"]
    if int(vision.get("epoch") or 0) <= 0 or len(vision.get("slots") or []) != 3:
        raise AcceptanceFailure("真实 Vision epoch 或三槽证据不完整")
    final_state = sections["final_state"]
    if str(sections["recommendation"].get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实推荐 generation 不一致")
    if str(final_state.get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实渲染 generation 不一致")
    if int(final_state.get("vision_epoch") or 0) != int(vision.get("epoch") or 0):
        raise AcceptanceFailure("真实渲染 Vision epoch 不一致")
    if not final_state.get("should_show") or final_state.get("presentation_mode") != "content":
        raise AcceptanceFailure("真实 Overlay 最终未进入可见内容态")
    schema_version = int(payload.get("schema_version") or 1)
    selection_revision = 0
    render_signature = ""
    if schema_version >= 2:
        render = payload.get("render")
        if not isinstance(render, dict):
            raise AcceptanceFailure("真实 Overlay v2 缺少 render 证据")
        if str(render.get("session_id") or "") != session_id:
            raise AcceptanceFailure("真实会话 session 不一致：render")
        if str(render.get("generation_id") or "") != generation_id:
            raise AcceptanceFailure("真实渲染 generation 不一致：render")
        if int(render.get("vision_epoch") or 0) != int(vision.get("epoch") or 0):
            raise AcceptanceFailure("真实渲染 Vision epoch 不一致：render")
        selection_revision = int(payload.get("selection_revision") or 0)
        if selection_revision <= 0 or int(render.get("selection_revision") or 0) != selection_revision:
            raise AcceptanceFailure("真实渲染 revision 不一致")
        rows = render.get("rows")
        if not isinstance(rows, list) or len(rows) != 3:
            raise AcceptanceFailure("真实 Overlay v2 缺少三槽 render rows")
        render_signature = str(payload.get("render_signature") or "")
        if not render_signature or str(render.get("render_signature") or "") != render_signature:
            raise AcceptanceFailure("真实 Overlay render signature 不一致")
        signature_payload = {
            "session_id": session_id,
            "generation_id": generation_id,
            "vision_epoch": int(vision.get("epoch") or 0),
            "selection_revision": selection_revision,
            "rows": rows,
        }
        raw_signature = json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        expected_signature = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()
        if render_signature != expected_signature:
            raise AcceptanceFailure("真实 Overlay render signature 无法复算")
    screenshot = Path(str(payload.get("screenshot") or ""))
    screenshot = screenshot if screenshot.is_absolute() else path.parent / screenshot
    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
        raise AcceptanceFailure("真实 Overlay 非空截图缺失")
    return {
        "generation_id": generation_id,
        "session_id": session_id,
        "vision_epoch": int(vision["epoch"]),
        "selection_revision": selection_revision,
        "render_signature": render_signature,
        "screenshot": str(screenshot),
    }


def verify_generation(root: Path) -> dict[str, Any]:
    """打开固定 generation，并交叉验证 manifest 计数和查询结果。"""

    view = DataSnapshotClient(root).open_view()
    champions = view.get_champions()
    details = [view.get_champion_detail(item["id"]) for item in champions]
    stat_record_count = sum(
        len(detail.get("augments", []))
        for detail in details
        if isinstance(detail, dict) and isinstance(detail.get("augments"), list)
    )
    manifest = view.manifest
    if len(champions) != manifest.champion_count:
        raise SnapshotValidationError("英雄数量与 generation manifest 不一致")
    if stat_record_count != manifest.stat_record_count:
        raise SnapshotValidationError("统计记录数与 generation manifest 不一致")
    return {
        "state": view.status()["state"],
        "generation_id": manifest.generation_id,
        "content_fingerprint": manifest.content_fingerprint,
        "champion_count": manifest.champion_count,
        "augment_count": manifest.augment_count,
        "stat_record_count": manifest.stat_record_count,
    }


def _source_evidence(
    source: str,
    pointer_payload: Mapping[str, Any],
    *,
    catalog_generation_id: str,
    catalog_sha256: str,
    expected_hero_ids: Sequence[str],
    strict: bool,
) -> tuple[dict[str, Any], SourceProvenance]:
    pointer = SourcePointerV2.from_mapping(pointer_payload)
    if pointer.catalog_generation_id != catalog_generation_id or pointer.catalog_sha256 != catalog_sha256:
        raise AcceptanceFailure(f"{source} 未绑定当前 Catalog cohort")
    run_root = source_run_dir(source, pointer.run_id)
    manifest_path = run_root / "manifest.json"
    report_path = run_root / "report.json"
    if sha256_file(manifest_path) != pointer.manifest_sha256:
        raise AcceptanceFailure(f"{source} manifest SHA-256 不一致")
    manifest = SourceRunManifestV2.from_mapping(_read_object(manifest_path))
    if not manifest.publishable or manifest.run_id != pointer.run_id or manifest.artifact != pointer.artifact:
        raise AcceptanceFailure(f"{source} manifest 与 current pointer 不一致")
    artifact_path = source_run_artifact_path(source, pointer.run_id, pointer.artifact.relative_path)
    if sha256_file(artifact_path) != pointer.artifact.sha256 or artifact_path.stat().st_size != pointer.artifact.size:
        raise AcceptanceFailure(f"{source} artifact SHA-256 或大小不一致")
    report = _read_object(report_path)

    if source == "aramkit":
        index = validate_scoped_stats_artifact(pointer_payload)
        outcomes = {item.item_id: item for item in manifest.outcomes}
        if (
            not outcomes
            or not set(outcomes).issubset(set(expected_hero_ids))
            or any(item.state != "success" for item in outcomes.values())
        ):
            raise AcceptanceFailure("ARAMKit outcome 未达到完整 success 或包含未知英雄")
        if pointer.artifact.record_count != int(index.get("record_count") or -1):
            raise AcceptanceFailure("ARAMKit 索引记录数与 pointer record_count 不一致")
        validation = {
            "expected_champions": len(outcomes),
            "successful_champions": len(outcomes),
            "confirmed_empty_champions": 0,
            "record_count": int(index["record_count"]),
            "catalog_champion_count": len(expected_hero_ids),
        }
    elif source == "blitz":
        payload = validate_blitz_artifact(pointer_payload)
        if pointer.artifact.record_count != len(payload["rows"]):
            raise AcceptanceFailure("Blitz 记录数与 pointer 不一致")
        validation = {
            "record_count": len(payload["rows"]),
            "patch": payload["patch"],
            "data_date": payload["data_date"],
        }
    elif source == "apex":
        payload = _read_object(artifact_path)
        validation = validate_apex_run(
            payload,
            manifest.outcomes,
            expected_champion_ids=expected_hero_ids,
            automated_min_ratio=0.0,
        )
        validate_apex_removal_evidence(report.get("removals"))
        if pointer.artifact.record_count != validation["record_count"]:
            raise AcceptanceFailure("Apex 记录数与 pointer 不一致")
    else:
        payload = _read_object(artifact_path)
        validation = validate_mayhem_run(payload, report, automated_min_ratio=0.0)
        validate_mayhem_removal_evidence(report.get("removals"))
        if pointer.artifact.record_count != validation["valid_items"]:
            raise AcceptanceFailure("Mayhem 有效 combo 数与 pointer 不一致")

    if strict and report.get("fallback"):
        raise AcceptanceFailure(f"{source} strict 验收不接受 fallback")
    provenance = SourceProvenance(
        source=source,  # type: ignore[arg-type]
        run_id=pointer.run_id,
        catalog_generation_id=pointer.catalog_generation_id,
        artifact_role=pointer.artifact.role,
        artifact_sha256=pointer.artifact.sha256,
        record_count=pointer.artifact.record_count,
        manifest_sha256=pointer.manifest_sha256,
        content_schema_version=pointer.artifact.content_schema_version,
    )
    return {
        "state": "ready",
        "run_id": pointer.run_id,
        "record_count": pointer.artifact.record_count,
        "sha256": pointer.artifact.sha256,
        "manifest_sha256": pointer.manifest_sha256,
        "last_success_at": pointer.last_success_at,
        **validation,
    }, provenance


def verify_sources(
    *,
    strict: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """验证正式 runtime Catalog 与全部来源 current。"""

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    resolved = CohortPromotionStore().consistent_pointers() if strict else {}
    catalog_pointer = resolved.get("catalog", {}) if strict else {}
    catalog = load_runtime_catalog_from_pointer(catalog_pointer) if strict else None
    if strict and catalog is None:
        raise AcceptanceFailure("正式 runtime Catalog current.v2.json 缺失或无效")
    if catalog is not None:
        adoption_evidence: dict[str, Any] = {}
        try:
            _assert_fresh(
                catalog_pointer.get("last_success_at"),
                SOURCE_INTERVALS["catalog"] * STALE_AGE_FACTOR,
                label="catalog",
                now=current_time,
            )
        except AcceptanceFailure:
            adoption_evidence = _blocked_adoption_evidence(catalog_pointer)
            if not adoption_evidence:
                raise
        expected_hero_ids = sorted(load_champion_core_data(catalog.root))
        if not expected_hero_ids:
            raise AcceptanceFailure("Catalog 英雄闭集为空")
        catalog_generation_id = catalog.generation_id
        catalog_sha256 = catalog.content_sha256
    else:
        expected_hero_ids = []
        catalog_generation_id = ""
        catalog_sha256 = ""

    result: dict[str, Any] = {}
    provenance: list[SourceProvenance] = []
    missing: list[str] = []
    source_pointers: dict[str, dict[str, Any]] = {}
    for source in ACTIVE_SOURCES:
        pointer = dict(resolved.get(source, {})) if strict else load_source_current(source, verify_hash=True)
        if not pointer:
            missing.append(source)
            result[source] = {"state": "unavailable"}
            continue
        if strict:
            evidence, item = _source_evidence(
                source,
                pointer,
                catalog_generation_id=catalog_generation_id,
                catalog_sha256=catalog_sha256,
                expected_hero_ids=expected_hero_ids,
                strict=True,
            )
            try:
                _assert_fresh(
                    pointer.get("last_success_at"),
                    SOURCE_INTERVALS[source] * STALE_AGE_FACTOR,
                    label=source,
                    now=current_time,
                )
                evidence["freshness"] = "fresh"
            except AcceptanceFailure:
                if source not in OPTIONAL_SOURCES:
                    raise
                evidence["freshness"] = "stale"
            result[source] = evidence
            provenance.append(item)
            source_pointers[source] = pointer
        else:
            artifact = pointer.get("artifact") if isinstance(pointer.get("artifact"), Mapping) else {}
            result[source] = {
                "state": "ready",
                "run_id": str(pointer.get("run_id") or ""),
                "record_count": int(artifact.get("record_count") or pointer.get("record_count") or 0),
                "sha256": str(artifact.get("sha256") or pointer.get("sha256") or ""),
            }
    if missing:
        raise AcceptanceFailure(f"来源 current 缺失或无效：{', '.join(missing)}")
    if strict and catalog is not None:
        result["catalog"] = {
            "state": "ready",
            "catalog_generation_id": catalog.generation_id,
            "content_sha256": catalog.content_sha256,
            "champion_count": len(expected_hero_ids),
            "last_success_at": catalog_pointer.get("last_success_at"),
            "freshness": "adoption_held" if adoption_evidence else "fresh",
        }
        if adoption_evidence:
            result["catalog_adoption"] = adoption_evidence
        result["_provenance"] = [*catalog.provenance(), *provenance]
        result["_catalog_root"] = catalog.root
        result["_expected_hero_ids"] = expected_hero_ids
        result["_source_pointers"] = source_pointers
    return result


def _normalized_id(value: object) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def _values_equal(expected: object, actual: object) -> bool:
    try:
        expected_float = float(expected)
        actual_float = float(actual)
    except (TypeError, ValueError):
        return str(expected or "").strip() == str(actual or "").strip()
    return math.isfinite(expected_float) and math.isfinite(actual_float) and math.isclose(
        expected_float,
        actual_float,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def _verify_aramkit_champions(view, pointer: SourcePointerV2) -> int:
    index = validate_scoped_stats_artifact(pointer.to_dict())
    index_path = source_run_artifact_path(
        "aramkit",
        pointer.run_id,
        pointer.artifact.relative_path,
    )
    expected: dict[str, Mapping[str, Any]] = {}
    for descriptor in index["files"]:
        payload = _read_object(index_path.parent / str(descriptor["relative_path"]))
        champion = payload.get("champion")
        if not isinstance(champion, Mapping):
            raise AcceptanceFailure("ARAMKit 逐英雄投影无效")
        champion_id = _normalized_id(champion.get("id"))
        expected[champion_id] = champion
    actual: dict[str, Mapping[str, Any]] = {}
    for champion in view.get_champions():
        champion_id = _normalized_id(champion.get("id"))
        actual[champion_id] = champion
    if set(actual) != set(expected):
        raise AcceptanceFailure("generation 与 ARAMKit 英雄键不一致")
    for key, row in expected.items():
        item = actual[key]
        fields = {
            "win_rate": item.get("英雄胜率"),
            "pick_rate": item.get("英雄出场率"),
            "sample_count": item.get("sample_count"),
            "source_rank": item.get("source_rank"),
        }
        for field, value in fields.items():
            if not _values_equal(row[field], value):
                raise AcceptanceFailure(f"generation ARAMKit 英雄字段不一致：key={key} field={field}")
    return len(actual)


def _verify_blitz_generation(
    view,
    pointer: SourcePointerV2,
    *,
    expected_champion_ids: Sequence[str],
) -> int:
    payload = validate_blitz_artifact(pointer.to_dict())
    rows = {str(item["augment_id"]): item for item in payload["rows"]}
    hints = view.get_overlay_hints()
    source = hints.get("source") if isinstance(hints.get("source"), Mapping) else {}
    pool = source.get("production_augment_pool") if isinstance(source, Mapping) else {}
    pool_ids = {str(value) for value in pool.get("canonical_ids", [])} if isinstance(pool, Mapping) else set()
    expected_ids = set(rows).intersection(pool_ids)
    if not expected_ids:
        raise AcceptanceFailure("Blitz generation 缺少 production 排名")
    expected_heroes = {_normalized_id(value) for value in expected_champion_ids}
    if not expected_heroes:
        raise AcceptanceFailure("Blitz generation 缺少 Catalog 英雄口径")
    hint_map = hints.get("hints") if isinstance(hints, Mapping) else None
    if not isinstance(hint_map, Mapping):
        raise AcceptanceFailure("Blitz generation 缺少 Overlay hint")
    verified = 0
    for augment_id in expected_ids:
        hint = hint_map.get(augment_id)
        stats_by_champion = hint.get("stats_by_champion_id") if isinstance(hint, Mapping) else None
        if not isinstance(stats_by_champion, Mapping) or {
            _normalized_id(value) for value in stats_by_champion
        } != expected_heroes:
            raise AcceptanceFailure(f"generation 与 Blitz 英雄投影不一致：augment={augment_id}")
        row = rows[augment_id]
        champion_tiers = {str(value["champion_id"]): int(value["tier"]) for value in row["top_champions"]}
        for champion_id in expected_heroes:
            item = stats_by_champion.get(champion_id)
            if not isinstance(item, Mapping):
                raise AcceptanceFailure(
                    f"generation Blitz tier 缺失：hero={champion_id} augment={augment_id}"
                )
            if (
                int(item.get("source_tier") or 0) != int(row["tier"])
                or item.get("champion_tier") != champion_tiers.get(champion_id)
                or str(item.get("source_patch") or "") != str(payload["patch"])
                or str(item.get("source_date") or "") != str(payload["data_date"])
            ):
                raise AcceptanceFailure(f"generation Blitz tier 字段不一致：hero={champion_id} augment={augment_id}")
            verified += 1
    return verified


def _verify_synergy_generation(view, apex_path: Path, mayhem_path: Path, catalog_root: Path) -> int:
    from hextech.modules.acquisition.mayhem.merge import merge_mayhem_combos

    summary = merge_mayhem_combos(
        apex_path=apex_path,
        mayhem_raw_path=mayhem_path,
        augment_manifest_path=catalog_root / "海克斯资源目录.v1.json",
        core_data_path=catalog_root / "英雄目录.v1.json",
        write_output=False,
    )
    expected = summary.get("merged_payload")
    if not isinstance(expected, Mapping) or not expected:
        raise AcceptanceFailure("Apex/Mayhem dry-run 合并结果为空")
    actual: dict[str, Any] = {}
    for champion in view.get_champions():
        champion_id = _normalized_id(champion.get("id"))
        champion_name = str(champion.get("name") or "")
        detail = view.get_champion_detail(champion_id) or {}
        actual[champion_id] = detail.get("synergy") if isinstance(detail.get("synergy"), Mapping) else {}
        expected_entry = expected.get(champion_id) or expected.get(champion_name) or {}
        if json.dumps(actual[champion_id], ensure_ascii=False, sort_keys=True) != json.dumps(
            expected_entry,
            ensure_ascii=False,
            sort_keys=True,
        ):
            raise AcceptanceFailure(f"generation 联动与确定性合并不一致：hero={champion_id}")
    return sum(
        len(value.get("synergy_items") or [])
        for value in actual.values()
        if isinstance(value, Mapping)
    )


def _verify_overlay_sample(view) -> dict[str, Any]:
    hints = view.get_overlay_hints()
    hint_map = hints.get("hints") if isinstance(hints.get("hints"), Mapping) else hints.get("augments", {})
    if not isinstance(hint_map, Mapping):
        raise AcceptanceFailure("Overlay hints 结构无效")
    for champion in view.get_champions():
        champion_id = _normalized_id(champion.get("id"))
        detail = view.get_champion_detail(champion_id) or {}
        cards = [item for item in detail.get("augments", []) if isinstance(item, Mapping)]
        synergy = detail.get("synergy") if isinstance(detail.get("synergy"), Mapping) else {}
        synergy_items = synergy.get("synergy_items") if isinstance(synergy, Mapping) else []
        synergy_names = {
            str(name).strip()
            for item in synergy_items if isinstance(item, Mapping)
            for name in (item.get("augment_names") or [])
            if str(name).strip()
        }
        matching = [item for item in cards if str(item.get("海克斯名称") or item.get("name") or "") in synergy_names]
        if not matching or len(cards) < 3:
            continue
        selected = [matching[0], *[item for item in cards if item is not matching[0]][:2]]
        statuses: list[str] = []
        for item in selected:
            augment_id = _normalized_id(item.get("id"))
            if view.get_combo_stats(champion_id, augment_id) is None:
                raise AcceptanceFailure(f"Overlay 样本统计不可追溯：hero={champion_id} augment={augment_id}")
            if augment_id not in hint_map:
                raise AcceptanceFailure(f"Overlay 样本 hint 缺失：augment={augment_id}")
            name = str(item.get("海克斯名称") or item.get("name") or "")
            statuses.append("READY" if name in synergy_names else "NO_MATCH")
        return {
            "generation_id": view.manifest.generation_id,
            "champion_id": champion_id,
            "augment_ids": [_normalized_id(item.get("id")) for item in selected],
            "statuses": statuses,
            "stat_count": len(selected),
            "synergy_count": statuses.count("READY"),
        }
    raise AcceptanceFailure("无法从 generation 选择三张有统计且至少一张有联动的 Overlay 样本")


def verify_strict_full_chain(snapshot_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    source_report = verify_sources(strict=True, now=now)
    provenance = source_report.pop("_provenance")
    catalog_root = source_report.pop("_catalog_root")
    raw_pointers = source_report.pop("_source_pointers")
    expected_hero_ids = source_report.pop("_expected_hero_ids")
    if not isinstance(provenance, list) or not all(isinstance(item, SourceProvenance) for item in provenance):
        raise AcceptanceFailure("strict provenance 无效")
    validate_complete_provenance(provenance)

    client = DataSnapshotClient(snapshot_root)
    view = client.open_view()
    _validate_generation_freshness(view.status(now=now), source_report)
    validate_complete_provenance(view.manifest.source_files)
    expected_fingerprint = content_fingerprint(provenance)
    if view.manifest.content_fingerprint != expected_fingerprint:
        raise AcceptanceFailure("generation content_fingerprint 与当前 cohort 内容不一致")

    pointers = {source: SourcePointerV2.from_mapping(raw_pointers[source]) for source in ACTIVE_SOURCES}
    apex_path = source_run_artifact_path("apex", pointers["apex"].run_id, pointers["apex"].artifact.relative_path)
    mayhem_path = source_run_artifact_path(
        "mayhem",
        pointers["mayhem"].run_id,
        pointers["mayhem"].artifact.relative_path,
    )
    champion_rows = _verify_aramkit_champions(view, pointers["aramkit"])
    ranking_rows = _verify_blitz_generation(
        view,
        pointers["blitz"],
        expected_champion_ids=expected_hero_ids,
    )
    synergy_rows = _verify_synergy_generation(view, apex_path, mayhem_path, Path(catalog_root))
    overlay = _verify_overlay_sample(view)
    generation = verify_generation(snapshot_root)
    generation.update({"aramkit_champions_verified": champion_rows, "blitz_rankings_verified": ranking_rows, "synergy_items_verified": synergy_rows})
    return {"catalog": source_report.pop("catalog"), "sources": source_report, "generation": generation, "overlay": overlay}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 Hextech v2 完整 generation 和来源 cohort。")
    parser.add_argument("--runtime", action="store_true", help="验证 var/snapshots，而不是 resources/seeds。")
    parser.add_argument("--require-sources", action="store_true", help="同时要求三个来源 current 和 artifact 有效。")
    parser.add_argument("--strict-full-chain", action="store_true", help="执行动态 Catalog 的严格全链验收。")
    parser.add_argument("--report-dir", type=Path, help="写入统一 JSON 验收报告的目录。")
    return parser


def _write_report(report_dir: Path | None, summary: Mapping[str, Any]) -> None:
    if report_dir is None:
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(
        json.dumps(dict(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # ``--runtime`` 必须与 Catalog/source current 使用同一个可变数据根。
    # 否则设置 HEXTECH_VAR_DIR 后会把真实来源 pointer 与 worktree 的旧
    # ``var/snapshots`` 交叉比较，制造并不存在的 cohort fingerprint 失败。
    snapshot_root = default_snapshot_root() if args.runtime else RUN_DIR / "resources/seeds"
    try:
        if args.strict_full_chain:
            if not args.runtime:
                raise AcceptanceFailure("--strict-full-chain 必须配合 --runtime")
            summary: dict[str, Any] = verify_strict_full_chain(snapshot_root)
        else:
            summary = {"generation": verify_generation(snapshot_root)}
            if args.require_sources:
                summary["sources"] = verify_sources()
        summary["passed"] = True
    except (OSError, RuntimeError, ValueError, SnapshotValidationError) as exc:
        summary = {"passed": False, "error": str(exc), "error_type": exc.__class__.__name__}
    _write_report(args.report_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
