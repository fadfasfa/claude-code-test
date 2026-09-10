"""Catalog v2 candidate 构建与 runtime generation 发布。

远端只更新英雄闭集和版本；海克斯目录沿用当前 Catalog 的已验证稳定目录，避免
第三方元数据缺失时把完整描述降级。候选先写 immutable generation，DataService
的 promotion journal 负责在三来源刷新成功后决定保留或回滚 pointer。
"""

from __future__ import annotations

import json
import hashlib
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import requests

from hextech.modules.data.catalog.versioned import (
    CATALOG_FILES,
    build_catalog_manifest,
    catalog_root,
    load_active_catalog,
    sha256_file,
    validate_catalog_files,
)
from hextech.modules.data.catalog.version_catalog import load_apexlol_slug_map
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.infrastructure.transport.scrapling_client import fetch_text
from hextech.modules.acquisition.common.contracts import utc_now_iso
from hextech.modules.acquisition.common.icons import normalize_safe_augment_icon_filename
from hextech.modules.vision.image_validation import is_valid_png_bytes


DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
JADE_MODE_PREFIX = "Jade_"
SOURCE_FILTER_SAMPLE_LIMIT = 10
CDRAGON_AUGMENTS_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/cherry-augments.json"
)
HEXTECH_METADATA_URL = "https://aramgg.com/data/aram-mayhem-augments.zh_cn.json"
# CDragon 的 manifest 路径以 ``lol-game-data/assets/`` 为前缀，但静态文件实际
# 位于 ``game/assets/``；两者混用会让 Kiwi/Cherry 图标全部得到 404。
CDRAGON_ASSET_BASE_URL = "https://raw.communitydragon.org/latest/game/assets/"
RARITY_TO_TIER = {
    "kBronze": "白银",
    "kSilver": "白银",
    "kGold": "黄金",
    "kPrismatic": "棱彩",
    "kEventChoice": "棱彩",
}


class CatalogRefreshError(RuntimeError):
    pass


def _load_json_result(url: str, *, timeout_ms: int) -> Any:
    result = fetch_text(url, timeout_ms=timeout_ms)
    if result.error or result.status_code != 200 or not result.text:
        raise CatalogRefreshError(
            f"Catalog 请求失败：url={url} status={result.status_code} error={result.error or result.error_kind}"
        )
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise CatalogRefreshError(f"Catalog JSON 无效：{url}") from exc


def _existing_aliases(root: Path) -> dict[str, list[str]]:
    try:
        payload = json.loads((root / "英雄目录.v1.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    entries = payload.get("aliases") if isinstance(payload, Mapping) else None
    result: dict[str, list[str]] = {}
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, Mapping):
            continue
        hero_id = str(item.get("heroId") or "")
        aliases = item.get("aliases")
        if hero_id and isinstance(aliases, list):
            result[hero_id] = [str(value) for value in aliases if str(value).strip()]
    return result


def _champion_catalog_payload_with_filter(
    data: Mapping[str, Any],
    previous_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    aliases_by_id = _existing_aliases(previous_root)
    entries: list[dict[str, Any]] = []
    excluded_ids: list[str] = []
    for raw in data.values():
        if not isinstance(raw, Mapping):
            continue
        hero_id = str(raw.get("key") or "").strip()
        hero_name = str(raw.get("name") or "").strip()
        title = str(raw.get("title") or "").strip()
        en_name = str(raw.get("id") or "").strip()
        if not hero_id or not hero_name or not en_name:
            continue
        # Data Dragon 16.15.1 开始把 Jade 模式变体混入 champion.json。这些条目
        # 与普通英雄共享身份，却没有独立的 Hextech/Apex 统计页，不能进入规范英雄闭集。
        if en_name.startswith(JADE_MODE_PREFIX):
            excluded_ids.append(hero_id)
            continue
        entries.append(
            {
                "heroName": hero_name,
                "title": title,
                "enName": en_name,
                "heroId": hero_id,
                "aliases": aliases_by_id.get(hero_id, []),
            }
        )
    entries.sort(key=lambda item: int(item["heroId"]))
    if not entries:
        raise CatalogRefreshError("Data Dragon 英雄闭集为空")

    alias_to_id: dict[str, str] = {}
    id_to_name: dict[str, dict[str, str]] = {}
    id_to_detail: dict[str, str] = {}

    def add_alias(value: object, hero_id: str) -> None:
        alias = str(value or "").strip()
        if not alias:
            return
        for candidate in (alias, alias.casefold()):
            existing = alias_to_id.get(candidate)
            if existing in (None, hero_id):
                alias_to_id[candidate] = hero_id

    for entry in entries:
        hero_id = str(entry["heroId"])
        hero_name = str(entry["heroName"])
        title = str(entry["title"])
        en_name = str(entry["enName"])
        id_to_name[hero_id] = {
            "heroName": hero_name,
            "enName": en_name,
            "title": title,
        }
        id_to_detail[hero_id] = hero_name
        for alias in (hero_name, title, en_name, *entry["aliases"]):
            add_alias(alias, hero_id)
    payload = {
        "schema_version": 1,
        "description": "英雄别名、ID、名称和详情的统一目录。",
        "aliases": entries,
        "alias_to_id": alias_to_id,
        "id_to_name": id_to_name,
        "id_to_detail": id_to_detail,
    }
    excluded_ids.sort(key=lambda value: int(value) if value.isdigit() else 10**9)
    source_filter = {
        "schema_version": 1,
        "upstream_entry_count": len(data),
        "canonical_entry_count": len(entries),
        "excluded_entry_count": len(excluded_ids),
        "excluded_reasons": {"jade_mode_variant": len(excluded_ids)} if excluded_ids else {},
        "excluded_sample_ids": excluded_ids[:SOURCE_FILTER_SAMPLE_LIMIT],
    }
    return payload, source_filter


def _champion_catalog_payload(data: Mapping[str, Any], previous_root: Path) -> dict[str, Any]:
    """兼容纯 payload 调用；来源过滤诊断由 refresh_catalog 单独发布。"""

    payload, _source_filter = _champion_catalog_payload_with_filter(data, previous_root)
    return payload


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_asset_url(icon_path: str) -> str:
    lowered = str(icon_path or "").replace("\\", "/").lstrip("/").lower()
    for prefix in ("lol-game-data/assets/", "assets/"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
    if lowered.startswith("assets/"):
        lowered = lowered[len("assets/") :]
    return CDRAGON_ASSET_BASE_URL + quote(lowered, safe="/._-") if lowered else ""


def _cdragon_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = _clean_text(raw.get("nameTRA") or raw.get("simpleNameTRA"))
    icon_path = _clean_text(raw.get("augmentSmallIconPath"))
    filename = normalize_safe_augment_icon_filename(Path(icon_path).name)
    return {
        "schema_version": 2,
        "name": name,
        "tier": RARITY_TO_TIER.get(_clean_text(raw.get("rarity")), _clean_text(raw.get("rarity"))),
        "filename": filename,
        "local_path": "",
        "icon_url": "",
        "cdragon_id": raw.get("id"),
        "augment_name_id": _clean_text(raw.get("augmentNameId")),
        "source_icon_path": icon_path,
        "source_icon_url": _source_asset_url(icon_path),
        "source_schema": "cdragon_minimal",
    }


def _build_cdragon_catalog(raw_items: list[Mapping[str, Any]], previous_root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        entry = _cdragon_entry(raw)
        # 正数 ID 是生产身份；-1 是 CDragon 的池外审计占位，必须改用
        # augmentNameId 才能保留 Strawberry/Special 等全部资源。
        source_id = str(entry["cdragon_id"] or "")
        stable_key = source_id if source_id != "-1" else str(entry["augment_name_id"] or "")
        if not entry["name"] or not stable_key:
            continue
        if stable_key in seen:
            raise CatalogRefreshError(f"CDragon Catalog 身份重复：{stable_key}")
        entries.append(entry)
        seen.add(stable_key)
    entries.sort(key=lambda item: (_clean_text(item["name"]).casefold(), str(item["augment_name_id"])))
    try:
        slug_map = load_apexlol_slug_map(previous_root)
    except (OSError, ValueError):
        slug_map = {}
    return {
        "schema_version": 2,
        "description": "CommunityDragon 完整海克斯资源审计目录；生产排名由 generation pool 单独约束。",
        "entries": entries,
        "name_to_icon": {},
        "apexlol_slug_map": slug_map,
    }


def _enabled_metadata_ids(payload: Mapping[str, Any]) -> set[str]:
    return {
        str(raw_id).strip()
        for raw_id, raw in payload.items()
        if isinstance(raw, Mapping)
        and bool(raw.get("enabled", True))
        and _clean_text(raw.get("displayName") or raw.get("name"))
    }


def _read_active_icon(entry: Mapping[str, Any], active_root: Path) -> bytes | None:
    """只复用 canonical ID、源路径和 SHA 都匹配的 active immutable asset。"""

    try:
        payload = json.loads((active_root / "augment_assets.v1.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    candidates = entries if isinstance(entries, list) else []
    canonical_id = str(entry.get("cdragon_id") or "")
    source_icon_path = str(entry.get("source_icon_path") or "")
    matches = [
        item
        for item in candidates
        if isinstance(item, Mapping)
        and str(item.get("canonical_id") or "") == canonical_id
        and str(item.get("source_icon_path") or "") == source_icon_path
    ]
    if len(matches) != 1:
        return None
    descriptor = matches[0]
    relative_path = str(descriptor.get("relative_path") or "")
    try:
        root = active_root.resolve()
        path = (root / relative_path).resolve()
        if root not in path.parents or not path.is_file():
            return None
        content = path.read_bytes()
        expected_size = int(descriptor.get("size") or 0)
    except (OSError, TypeError, ValueError):
        return None
    if (
        expected_size != len(content)
        or hashlib.sha256(content).hexdigest() != str(descriptor.get("sha256") or "")
        or not is_valid_png_bytes(content)
    ):
        return None
    return content


def _fetch_icon(
    entry: Mapping[str, Any],
    active_root: Path,
    *,
    stop_event: threading.Event | None = None,
    request_get: Callable[..., Any] = requests.get,
    sleep_func: Callable[[float], None] = time.sleep,
) -> bytes:
    cached = _read_active_icon(entry, active_root)
    if cached is not None:
        return cached
    url = str(entry.get("source_icon_url") or "")
    if not url:
        raise CatalogRefreshError(f"CDragon 图标 URL 缺失：{entry.get('cdragon_id')}")
    delays = (0.5, 1.5)
    last_error = ""
    for attempt in range(3):
        if stop_event is not None and stop_event.is_set():
            raise CatalogRefreshError("catalog_refresh_cancelled")
        try:
            response = request_get(url, timeout=(5, 20))
            status_code = int(getattr(response, "status_code", 0) or 0)
            retryable_status = status_code in {408, 425, 429} or 500 <= status_code <= 599
            if retryable_status:
                last_error = f"HTTP {status_code}"
                if attempt >= 2:
                    break
            elif status_code >= 400:
                raise CatalogRefreshError(
                    f"CDragon 图标请求不可重试：id={entry.get('cdragon_id')} status={status_code}"
                )
            else:
                content = bytes(response.content)
                if not is_valid_png_bytes(content):
                    raise CatalogRefreshError(f"CDragon 图标不是有效 PNG：{entry.get('cdragon_id')}")
                return content
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = type(exc).__name__
            if attempt >= 2:
                break
        if attempt < 2:
            delay = delays[attempt]
            if stop_event is not None:
                if stop_event.wait(delay):
                    raise CatalogRefreshError("catalog_refresh_cancelled")
            else:
                sleep_func(delay)
    raise CatalogRefreshError(
        f"CDragon 图标请求重试耗尽：id={entry.get('cdragon_id')} reason={last_error or 'unknown'}"
    )


def _freeze_enabled_assets(
    staging: Path,
    catalog_payload: dict[str, Any],
    enabled_ids: set[str],
    active_root: Path,
    *,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    entries = catalog_payload.get("entries") if isinstance(catalog_payload.get("entries"), list) else []
    selected = [entry for entry in entries if isinstance(entry, dict) and str(entry.get("cdragon_id") or "") in enabled_ids]
    resolved = {str(entry.get("cdragon_id") or "") for entry in selected}
    duplicate_ids = sorted(
        source_id
        for source_id in resolved
        if sum(str(entry.get("cdragon_id") or "") == source_id for entry in selected) != 1
    )
    if resolved != enabled_ids or duplicate_ids:
        missing = sorted(enabled_ids.difference(resolved))[:SOURCE_FILTER_SAMPLE_LIMIT]
        raise CatalogRefreshError(
            f"CDragon 启用身份映射不完整：missing={missing} "
            f"duplicates={duplicate_ids[:SOURCE_FILTER_SAMPLE_LIMIT]}"
        )

    downloaded: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="catalog-icons") as executor:
        futures = {
            executor.submit(_fetch_icon, entry, active_root, stop_event=stop_event): entry
            for entry in selected
        }
        for future in as_completed(futures):
            entry = futures[future]
            downloaded[str(entry.get("cdragon_id") or "")] = future.result()

    asset_entries: list[dict[str, Any]] = []
    name_to_icon: dict[str, str] = {}
    for entry in selected:
        source_id = str(entry.get("cdragon_id") or "")
        content = downloaded[source_id]
        digest = hashlib.sha256(content).hexdigest()
        relative = f"assets/augments/{digest}.png"
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
        entry["filename"] = f"{digest}.png"
        entry["local_path"] = relative
        entry["icon_url"] = f"/{relative}"
        entry["icon_sha256"] = digest
        name_to_icon[str(entry["name"])] = f"/{relative}"
        asset_entries.append(
            {
                "canonical_id": source_id,
                "augment_name_id": str(entry.get("augment_name_id") or ""),
                "relative_path": relative,
                "sha256": digest,
                "size": len(content),
                "source_icon_path": str(entry.get("source_icon_path") or ""),
            }
        )
    catalog_payload["name_to_icon"] = dict(sorted(name_to_icon.items()))
    asset_entries.sort(key=lambda item: int(item["canonical_id"]) if item["canonical_id"].isdigit() else 10**9)
    return {"schema_version": 1, "entries": asset_entries}


def _write_candidate(
    root: Path,
    *,
    allow_remote: bool,
    stop_event: threading.Event | None = None,
) -> tuple[Path, bool, dict[str, Any]]:
    active = load_active_catalog()
    source_filter: dict[str, Any] = {}
    staging = catalog_root() / "staging" / f"catalog-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        for _role, filename, _list_key in CATALOG_FILES:
            shutil.copy2(active.root / filename, staging / filename)
        if allow_remote:
            versions = _load_json_result(DDRAGON_VERSIONS_URL, timeout_ms=10_000)
            if not isinstance(versions, list) or not versions or not str(versions[0]).strip():
                raise CatalogRefreshError("Data Dragon versions 为空")
            version = str(versions[0]).strip()
            champions = _load_json_result(
                f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json",
                timeout_ms=15_000,
            )
            data = champions.get("data") if isinstance(champions, Mapping) else None
            if not isinstance(data, Mapping):
                raise CatalogRefreshError("Data Dragon champion.data 缺失")
            champion_payload, source_filter = _champion_catalog_payload_with_filter(data, active.root)
            atomic_write_json(
                staging / "英雄目录.v1.json",
                champion_payload,
                ensure_ascii=False,
                indent=2,
            )
            (staging / "hero_version.txt").write_text(version, encoding="utf-8")
            cdragon = _load_json_result(CDRAGON_AUGMENTS_URL, timeout_ms=20_000)
            metadata = _load_json_result(HEXTECH_METADATA_URL, timeout_ms=10_000)
            if not isinstance(cdragon, list) or not isinstance(metadata, Mapping):
                raise CatalogRefreshError("CDragon/Hextech metadata schema 无效")
            catalog_payload = _build_cdragon_catalog(
                [item for item in cdragon if isinstance(item, Mapping)],
                active.root,
            )
            enabled_ids = _enabled_metadata_ids(metadata)
            asset_payload = _freeze_enabled_assets(
                staging,
                catalog_payload,
                enabled_ids,
                active.root,
                stop_event=stop_event,
            )
            atomic_write_json(staging / "海克斯资源目录.v1.json", catalog_payload, ensure_ascii=False, indent=2)
            atomic_write_json(staging / "augment_assets.v1.json", asset_payload, ensure_ascii=False, indent=2)
            source_filter["augments"] = {
                "schema_version": 1,
                "upstream_entry_count": len(cdragon),
                "metadata_entry_count": len(metadata),
                "enabled_entry_count": len(enabled_ids),
                "frozen_asset_count": len(asset_payload["entries"]),
            }
        manifest = build_catalog_manifest(staging, created_at=utc_now_iso())
        validate_catalog_files(staging, manifest)
        atomic_write_json(staging / "manifest.json", manifest.to_dict(), ensure_ascii=False, indent=2)
        return staging, manifest.content_sha256 != active.content_sha256, source_filter
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def refresh_catalog(
    *,
    force: bool = False,
    allow_remote: bool = True,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    if promote_current:
        raise CatalogRefreshError("正式 Catalog current 只能由 cohort promotion 切换")
    staging, changed, source_filter = _write_candidate(
        catalog_root(),
        allow_remote=allow_remote,
        stop_event=stop_event,
    )
    try:
        payload = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        generation_id = str(payload["catalog_generation_id"])
        final = catalog_root() / "generations" / generation_id
        if not final.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(final)
        else:
            shutil.rmtree(staging, ignore_errors=True)
        manifest_path = final / "manifest.json"
        now = utc_now_iso()
        pointer = {
            "schema_version": 2,
            "catalog_generation_id": generation_id,
            "content_sha256": str(payload["content_sha256"]),
            "manifest_sha256": sha256_file(manifest_path),
            "completed_at": str(payload["created_at"]),
            "last_success_at": now,
        }
        if pointer_output is not None:
            atomic_write_json(Path(pointer_output), pointer, ensure_ascii=False, indent=2)
        return {
            "state": "ready",
            "changed": changed,
            "catalog_generation_id": generation_id,
            "content_sha256": str(payload["content_sha256"]),
            "forced": bool(force),
            "source_filter": source_filter,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


__all__ = ["CatalogRefreshError", "refresh_catalog"]
