"""Catalog v2 candidate 构建与 runtime generation 发布。

远端只更新英雄闭集和版本；海克斯目录沿用当前 Catalog 的已验证稳定目录，避免
第三方元数据缺失时把完整描述降级。候选先写 immutable generation，DataService
的 promotion journal 负责在三来源刷新成功后决定保留或回滚 pointer。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.catalog.versioned import (
    CATALOG_FILES,
    build_catalog_manifest,
    catalog_root,
    load_active_catalog,
    sha256_file,
    validate_catalog_files,
)
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.infrastructure.transport.scrapling_client import fetch_text
from hextech.modules.acquisition.common.contracts import utc_now_iso


DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"


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


def _champion_catalog_payload(data: Mapping[str, Any], previous_root: Path) -> dict[str, Any]:
    aliases_by_id = _existing_aliases(previous_root)
    entries: list[dict[str, Any]] = []
    for raw in data.values():
        if not isinstance(raw, Mapping):
            continue
        hero_id = str(raw.get("key") or "").strip()
        hero_name = str(raw.get("name") or "").strip()
        title = str(raw.get("title") or "").strip()
        en_name = str(raw.get("id") or "").strip()
        if not hero_id or not hero_name or not en_name:
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
    return {
        "schema_version": 1,
        "description": "英雄别名、ID、名称和详情的统一目录。",
        "aliases": entries,
        "alias_to_id": alias_to_id,
        "id_to_name": id_to_name,
        "id_to_detail": id_to_detail,
    }


def _write_candidate(root: Path, *, allow_remote: bool) -> tuple[Path, bool]:
    active = load_active_catalog()
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
            atomic_write_json(
                staging / "英雄目录.v1.json",
                _champion_catalog_payload(data, active.root),
                ensure_ascii=False,
                indent=2,
            )
            (staging / "hero_version.txt").write_text(version, encoding="utf-8")
        manifest = build_catalog_manifest(staging, created_at=utc_now_iso())
        validate_catalog_files(staging, manifest)
        atomic_write_json(staging / "manifest.json", manifest.to_dict(), ensure_ascii=False, indent=2)
        return staging, manifest.content_sha256 != active.content_sha256
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def refresh_catalog(
    *,
    force: bool = False,
    allow_remote: bool = True,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> dict[str, Any]:
    if promote_current:
        raise CatalogRefreshError("正式 Catalog current 只能由 cohort promotion 切换")
    staging, changed = _write_candidate(catalog_root(), allow_remote=allow_remote)
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
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


__all__ = ["CatalogRefreshError", "refresh_catalog"]
