"""Apex 条件检查的稳定版本身份与 source-result helper。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

from hextech.infrastructure.sources.apex.common import (
    ApexPageState,
    ChampionInfo,
    FetchedResource,
    SynergyEntry,
    champion_detail_url,
    classify_apex_page,
    item_outcome,
)
from hextech.modules.data.source_runs import load_source_current, load_source_run_manifest
from hextech.infrastructure.transport.conditional_response import parse_retry_after_seconds


APEX_PROJECTION_REVISION = "apex-projection-v2"
APEX_REPEAT_INVALID_RETRY_SECONDS = 6 * 60 * 60


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upstream_revision(resources: Mapping[str, FetchedResource]) -> str:
    identities = [
        (
            resource.url,
            resource.body_sha256
            or hashlib.sha256(resource.text.encode("utf-8")).hexdigest(),
        )
        for resource in resources.values()
        if resource.text
    ]
    return canonical_sha256(sorted(identities)) if identities else ""


def resources_complete(
    expected_ids: Sequence[str], resources: Mapping[str, FetchedResource]
) -> bool:
    return len(resources) == len(expected_ids) and all(
        champion_id in resources
        and not resources[champion_id].error
        and resources[champion_id].status_code in {200, 304}
        and bool(resources[champion_id].text)
        for champion_id in expected_ids
    )


def retry_after_for_resources(
    resources: Mapping[str, FetchedResource], *, default: int
) -> int:
    server_values = [
        value
        for resource in resources.values()
        if (value := parse_retry_after_seconds(resource.response_headers)) is not None
    ]
    return max([default, *server_values])


def current_manifest() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        pointer = load_source_current("apex")
        manifest = load_source_run_manifest("apex", str(pointer.get("run_id") or ""))
    except (OSError, TypeError, ValueError):
        return {}, {}
    return pointer, dict(manifest.metadata) if manifest is not None else {}


def source_result_fields(
    *,
    check_status: str,
    upstream_revision: str,
    applied_revision: str = "",
    retry_after_seconds: int = 0,
) -> dict[str, object]:
    representation_revision = upstream_revision
    semantic_upstream = (
        applied_revision
        if check_status in {"up_to_date", "changed"} and applied_revision
        else upstream_revision
    )
    return {
        "check_status": check_status,
        "upstream_revision": semantic_upstream,
        "applied_revision": applied_revision,
        "representation_revision": representation_revision,
        "retry_after_seconds": max(0, int(retry_after_seconds)),
    }


def failure_fingerprint(
    upstream_revision_value: str,
    parser_revision: str,
    catalog_generation_id: str,
    catalog_sha256: str,
) -> str:
    if not upstream_revision_value:
        return ""
    return canonical_sha256(
        {
            "upstream_revision": upstream_revision_value,
            "parser_revision": parser_revision,
            "catalog_generation_id": catalog_generation_id,
            "catalog_sha256": catalog_sha256,
        }
    )


def up_to_date_result(
    *,
    current_pointer: Mapping[str, Any],
    dry_run: bool,
    upstream_revision_value: str,
    applied_revision: str,
    stats: Mapping[str, Any] | None = None,
    outcomes: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "run_id": str(current_pointer.get("run_id") or ""),
        "dry_run": dry_run,
        "published": False,
        "publishable": True,
        "reason": "not_stale",
        **source_result_fields(
            check_status="up_to_date",
            upstream_revision=upstream_revision_value,
            applied_revision=applied_revision,
        ),
    }
    if stats is not None:
        result["stats"] = dict(stats)
    if outcomes is not None:
        result["outcomes"] = list(outcomes)
    return result


def repeated_failure_result(
    *,
    run_id: str,
    dry_run: bool,
    upstream_revision_value: str,
    applied_revision: str,
    fingerprint: str,
    projection_identity: str,
    retry_after_seconds: int = APEX_REPEAT_INVALID_RETRY_SECONDS,
) -> dict[str, Any]:
    return {
        "success": False,
        "run_id": run_id,
        "dry_run": dry_run,
        "published": False,
        "publishable": False,
        "reason_code": "repeated_invalid_content",
        "failure_fingerprint": fingerprint,
        "failure_projection": projection_identity,
        **source_result_fields(
            check_status="unknown",
            upstream_revision=upstream_revision_value,
            applied_revision=applied_revision,
            retry_after_seconds=retry_after_seconds,
        ),
    }


def collect_resources(
    ordered_ids: Sequence[str],
    *,
    source: Any,
    slug_map: Mapping[str, str],
    stop: Any,
    current_context: Callable[[], Any],
    on_progress: Callable[[int, int, str], None] | None,
    delay: float,
) -> dict[str, FetchedResource]:
    resources: dict[str, FetchedResource] = {}
    pending = list(ordered_ids)
    completed = 0
    while pending and not stop.is_set():
        latest = current_context()
        priority = str(latest.champion_id)
        if priority in pending:
            champion_id = priority
        elif latest.pause_background:
            stop.wait(0.05)
            continue
        else:
            champion_id = pending[0]
        pending.remove(champion_id)
        url = champion_detail_url(source.base_url, slug_map[champion_id])
        resource = source.fetch(url, allow_browser=True)
        completed += 1
        if resource is not None:
            resources[champion_id] = resource
        if on_progress is not None:
            on_progress(completed, len(ordered_ids), "download")
        if delay and pending:
            stop.wait(delay)
    return resources


def project_resources(
    ordered_ids: Sequence[str],
    *,
    resources: Mapping[str, FetchedResource],
    core_info: Mapping[str, ChampionInfo],
    slug_map: Mapping[str, str],
    source_base_url: str,
    extractor: Any,
    extract: Callable[..., Any],
) -> tuple[list[Any], dict[str, list[SynergyEntry]]]:
    outcomes = []
    combined: dict[str, list[SynergyEntry]] = {}
    for champion_id in ordered_ids:
        resource = resources.get(champion_id)
        entries: list[SynergyEntry] = []
        extracted: dict[str, list[SynergyEntry]] = {}
        page = None
        if resource and resource.text and not resource.error:
            entries, extracted, page = extract(
                extractor,
                core_info[champion_id],
                resource,
                expected_slug=slug_map[champion_id],
            )
        if page is None:
            page = classify_apex_page(
                resource.text if resource else "",
                expected_slug=slug_map[champion_id],
                entry_count=len(entries),
                status_code=resource.status_code if resource else None,
            )
        outcomes.append(item_outcome(
            champion_id,
            page,
            record_count=len(entries),
            backend=resource.source if resource else "none",
            status_code=resource.status_code if resource else None,
            url=champion_detail_url(source_base_url, slug_map[champion_id]),
        ))
        if page.state is ApexPageState.HAS_SYNERGY:
            for key, values in extracted.items():
                combined.setdefault(key, []).extend(values)
    return outcomes, combined


__all__ = [
    "APEX_PROJECTION_REVISION",
    "APEX_REPEAT_INVALID_RETRY_SECONDS",
    "canonical_sha256",
    "collect_resources",
    "current_manifest",
    "failure_fingerprint",
    "project_resources",
    "resources_complete",
    "retry_after_for_resources",
    "repeated_failure_result",
    "source_result_fields",
    "up_to_date_result",
    "upstream_revision",
]
