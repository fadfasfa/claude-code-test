"""Vision 模板构建与元数据发布。

图片读取、指纹生成与真实名称样本只在冷构建期间发生。发布前会丢弃每条模板的
分散指纹数组，仅让 ``_RankMatrices`` 持有连续矩阵。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image

from hextech.infrastructure.vision.template_models import TemplateEntry, TemplateIndex, _RankMatrices
from hextech.modules.data.catalog.version_catalog import load_augment_name_to_icon_map
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries
from hextech.modules.data.catalog.versioned import load_runtime_catalog_from_pointer
from hextech.modules.data.ports.paths import ASSET_DIR, INDEX_DATA_DIR
from hextech.modules.recommendation.hints import (
    normalize_augment_id,
    normalize_augment_name,
    query_overlay_hint,
)


def _clean_text(value: Any, *, fallback: str = "") -> str:
    return " ".join(str(value or fallback).split()).strip()


def _load_manifest_entries(root: Path, *, use_runtime_resources: bool = True) -> list[Mapping[str, Any]]:
    from hextech.infrastructure.vision.sidecar_fingerprints import _load_manifest_entries as load_entries

    return load_entries(root, use_runtime_resources=use_runtime_resources)


def _load_manifest_entries_by_name(
    root: Path,
    *,
    use_runtime_resources: bool = True,
) -> dict[str, list[Mapping[str, Any]]]:
    from hextech.infrastructure.vision.sidecar_fingerprints import _load_manifest_entries_by_name as load_entries_by_name

    return load_entries_by_name(root, use_runtime_resources=use_runtime_resources)


def _select_manifest_item(
    manifest_by_name: Mapping[str, list[Mapping[str, Any]]],
    name: str,
    mapped_icon: str,
) -> Mapping[str, Any]:
    from hextech.infrastructure.vision.sidecar_fingerprints import _select_manifest_item as select_item

    return select_item(manifest_by_name, name, mapped_icon)


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    """复用现有图标/文字掩码构建器；返回仅供本阶段消费的 NumPy 指纹条目。"""

    from hextech.infrastructure.vision.sidecar_fingerprints import build_template_index as build_index

    return build_index(raw_templates)


def _attach_observed_name_exemplars(
    template_index: Sequence[TemplateEntry],
    asset_dir: Path,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    extra_aliases: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    strict: bool = False,
) -> list[TemplateEntry]:
    """绑定脱敏卡名 ROI；文件名别名必须解析到唯一 canonical ID。

    exemplar 文件名沿用上游 ARAM alias（例如 ``aram_dawnbringersresolve``），
    不能直接当作视觉模板的 canonical 数字 ID。这里复用同一份 hint alias
    索引；严格构建时未解析或一对多文件立即失败，避免静默得到零行矩阵。
    """

    from hextech.infrastructure.vision.sidecar_fingerprints import _normalized_fingerprint, _text_levels

    exemplar_dir = asset_dir / "vision" / "name_exemplars"
    fingerprints_by_id: dict[str, list[np.ndarray]] = {}
    fingerprints_seen: dict[str, set[bytes]] = {}
    aliases: dict[str, str] = {}
    conflicts: set[str] = set()

    def add_alias(raw: object, canonical_id: str) -> None:
        text = str(raw or "").strip()
        if not text or not canonical_id:
            return
        keys = {text, normalize_augment_id(text), normalize_augment_name(text)}
        for key in keys:
            if not key:
                continue
            existing = aliases.get(key)
            if existing and existing != canonical_id:
                conflicts.add(key)
            else:
                aliases[key] = canonical_id

    for entry in template_index:
        canonical_id = str(entry.augment_id or "").strip()
        add_alias(canonical_id, canonical_id)
        add_alias(entry.name, canonical_id)
        if hint_cache is not None and canonical_id:
            result = query_overlay_hint(hint_cache, canonical_id)
            hint = result.get("hint") if result.get("ok") else None
            if isinstance(hint, Mapping):
                add_alias(hint.get("name"), canonical_id)
                for alias in hint.get("aliases", ()) if isinstance(hint.get("aliases"), list) else ():
                    add_alias(alias, canonical_id)
    extra_pairs = (
        list(extra_aliases.items())
        if isinstance(extra_aliases, Mapping)
        else list(extra_aliases or ())
    )
    authority_aliases: dict[str, str] = {}
    authority_conflicts: set[str] = set()
    for raw_alias, canonical_id in extra_pairs:
        resolved_id = str(canonical_id or "").strip()
        if not resolved_id:
            continue
        text = str(raw_alias or "").strip()
        for key in {text, normalize_augment_id(text), normalize_augment_name(text)}:
            if not key:
                continue
            existing = authority_aliases.get(key)
            if existing and existing != resolved_id:
                authority_conflicts.add(key)
            else:
                authority_aliases[key] = resolved_id
    for key, resolved_id in authority_aliases.items():
        if key in authority_conflicts:
            conflicts.add(key)
            continue
        # Catalog/production identity 是 canonical 权威；允许它把资源模板中的
        # ARAM 临时 id 规范成数字 id，但权威源自身的一对多冲突绝不覆盖。
        aliases[key] = resolved_id
        conflicts.discard(key)

    unresolved: list[str] = []
    ambiguous: list[str] = []
    exemplar_files_by_id: dict[str, list[str]] = {}
    if exemplar_dir.is_dir():
        for path in sorted(exemplar_dir.glob("*.png")):
            raw_alias = path.stem.split("__", 1)[0]
            alias_keys = (raw_alias, normalize_augment_id(raw_alias), normalize_augment_name(raw_alias))
            if any(key and key in conflicts for key in alias_keys):
                ambiguous.append(path.name)
                continue
            resolved_ids = {
                aliases[key]
                for key in alias_keys
                if key and key in aliases and key not in conflicts
            }
            if len(resolved_ids) != 1:
                if len(resolved_ids) == 0:
                    unresolved.append(path.name)
                else:
                    ambiguous.append(path.name)
                continue
            augment_id = next(iter(resolved_ids))
            if not augment_id:
                continue
            exemplar_files_by_id.setdefault(augment_id, []).append(path.name)
            try:
                with Image.open(path) as opened:
                    fingerprint = _normalized_fingerprint(_text_levels(opened.convert("RGB")))
            except OSError:
                continue
            if fingerprint is None:
                continue
            array = np.ascontiguousarray(np.asarray(fingerprint, dtype=np.float16))
            digest = array.tobytes()
            seen = fingerprints_seen.setdefault(augment_id, set())
            if digest not in seen:
                seen.add(digest)
                fingerprints_by_id.setdefault(augment_id, []).append(array)
    template_ids = {
        aliases.get(str(entry.augment_id or "").strip())
        or aliases.get(normalize_augment_id(entry.augment_id))
        or str(entry.augment_id or "").strip()
        for entry in template_index
    }
    unbound = sorted(
        filename
        for augment_id, filenames in exemplar_files_by_id.items()
        if augment_id not in template_ids
        for filename in filenames
    )
    if strict and (unresolved or ambiguous or unbound):
        details: list[str] = []
        if unresolved:
            details.append(f"unresolved={unresolved[:8]}")
        if ambiguous:
            details.append(f"ambiguous={ambiguous[:8]}")
        if unbound:
            details.append(f"unbound={unbound[:8]}")
        raise ValueError("observed_name_exemplar_binding_failed " + " ".join(details))
    bound_entries: list[TemplateEntry] = []
    for entry in template_index:
        raw_id = str(entry.augment_id or "").strip()
        canonical_id = aliases.get(raw_id) or aliases.get(normalize_augment_id(raw_id)) or raw_id
        fingerprints = tuple(fingerprints_by_id.get(canonical_id, ()))
        bound_entries.append(
            replace(
                entry,
                augment_id=canonical_id if fingerprints and canonical_id.isdecimal() else entry.augment_id,
                observed_name_fingerprints=fingerprints,
            )
        )
    return bound_entries


def _production_pool(hint_cache: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    source = hint_cache.get("source") if isinstance(hint_cache, Mapping) else None
    pool = source.get("production_augment_pool") if isinstance(source, Mapping) else None
    return pool if isinstance(pool, Mapping) else None


def _production_catalog_root(pool: Mapping[str, Any]) -> Path:
    pointer = {
        "schema_version": 2,
        "catalog_generation_id": str(pool.get("catalog_generation_id") or ""),
        "content_sha256": str(pool.get("catalog_sha256") or ""),
        "manifest_sha256": str(pool.get("catalog_manifest_sha256") or ""),
    }
    view = load_runtime_catalog_from_pointer(pointer)
    if view is None or view.content_sha256 != pointer["content_sha256"]:
        raise ValueError("production_pool_catalog_unavailable")
    return view.root


def _load_production_pool_entries(
    pool: Mapping[str, Any],
    *,
    hint_cache: Mapping[str, Any] | None,
) -> list[TemplateEntry]:
    if str(pool.get("state") or "") != "ready":
        raise ValueError("production_pool_unavailable")
    identities = pool.get("identities")
    if not isinstance(identities, list) or not identities:
        raise ValueError("production_pool_unavailable")
    catalog_root = _production_catalog_root(pool)
    raw_templates: dict[str, dict[str, Any]] = {}
    for raw_identity in identities:
        if not isinstance(raw_identity, Mapping):
            continue
        canonical_id = str(raw_identity.get("canonical_id") or "").strip()
        name = _clean_text(raw_identity.get("name"))
        if not canonical_id or not canonical_id.isdecimal() or not name:
            raise ValueError("production_pool_identity_invalid")
        variants = raw_identity.get("visual_variants")
        images: list[Image.Image] = []
        filenames: list[str] = []
        for variant in variants if isinstance(variants, list) else ():
            if not isinstance(variant, Mapping):
                continue
            relative = str(variant.get("local_path") or "").replace("\\", "/").lstrip("/")
            path = (catalog_root / relative).resolve()
            if not relative or catalog_root.resolve() not in path.parents:
                raise ValueError(f"production_pool_asset_invalid:{canonical_id}")
            try:
                with Image.open(path) as opened:
                    images.append(opened.copy())
                filenames.append(path.name)
            except OSError as exc:
                raise ValueError(f"production_pool_asset_unavailable:{canonical_id}") from exc
        if not images:
            raise ValueError(f"production_pool_asset_unavailable:{canonical_id}")
        hint_result = query_overlay_hint(hint_cache or {}, canonical_id)
        hint_value = hint_result.get("hint")
        hint = hint_value if hint_result.get("ok") and isinstance(hint_value, Mapping) else {}
        raw_templates[canonical_id] = {
            "name": name,
            "tier": _clean_text(hint.get("tier") or raw_identity.get("tier"), fallback="Unknown"),
            "summary": _clean_text(hint.get("summary"), fallback="本地模板识别结果"),
            "images": images,
            "source_icon_filenames": filenames,
            "priority": 1 if hint_result.get("ok") else 0,
        }
    entries = _attach_observed_name_exemplars(
        build_template_index(raw_templates),
        Path(ASSET_DIR),
        hint_cache=hint_cache,
        extra_aliases=[
            (
                str(variant.get("augment_name_id") or ""),
                str(identity.get("canonical_id") or ""),
            )
            for identity in identities
            if isinstance(identity, Mapping)
            for variant in (identity.get("visual_variants") if isinstance(identity.get("visual_variants"), list) else [])
            if isinstance(variant, Mapping) and variant.get("augment_name_id")
        ],
        strict=True,
    )
    expected = {str(item.get("canonical_id") or "") for item in identities if isinstance(item, Mapping)}
    actual = {entry.augment_id for entry in entries}
    if expected != actual or len(entries) != len(expected):
        raise ValueError(f"production_pool_template_mismatch:expected={len(expected)} actual={len(actual)}")
    return entries


def load_default_template_entries(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    require_production_pool: bool = False,
) -> list[TemplateEntry]:
    """从本地 catalog/assets 构建临时模板条目，不触发网络请求。"""

    pool = _production_pool(hint_cache)
    if pool is not None:
        return _load_production_pool_entries(pool, hint_cache=hint_cache)
    if require_production_pool:
        raise ValueError("production_pool_unavailable")

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_data_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "resources" / "catalog"
    asset_dir = Path(ASSET_DIR) if use_runtime_resources else root / "resources" / "assets"
    try:
        name_to_icon = load_augment_name_to_icon_map(version_data_dir)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(name_to_icon, Mapping):
        return []

    manifest_entries = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    manifest_by_name = _load_manifest_entries_by_name(root, use_runtime_resources=use_runtime_resources)
    raw_templates: dict[str, dict[str, Any]] = {}
    names = {
        _clean_text(item.get("name"))
        for item in manifest_entries
        if _clean_text(item.get("name"))
    } | {_clean_text(name) for name in name_to_icon if _clean_text(name)}
    for name in sorted(names, key=normalize_augment_id):
        clean_name = _clean_text(name)
        manifest_items = manifest_by_name.get(clean_name) or manifest_by_name.get(normalize_augment_id(clean_name)) or []
        mapped_icon = str(name_to_icon.get(clean_name) or "")
        if not clean_name:
            continue
        hint_result = query_overlay_hint(hint_cache or {}, clean_name)
        hint_value = hint_result.get("hint")
        hint: Mapping[str, Any] = hint_value if hint_result.get("ok") and isinstance(hint_value, Mapping) else {}
        variants: list[tuple[Mapping[str, Any], list[str]]] = []
        for manifest_item in manifest_items:
            icon_path = str(manifest_item.get("local_path") or manifest_item.get("filename") or "")
            if icon_path:
                variants.append((manifest_item, [icon_path]))
        if not variants and mapped_icon:
            variants.append(({}, [mapped_icon]))

        for manifest_item, icon_paths in variants:
            images: list[Image.Image] = []
            filenames: list[str] = []
            loaded_paths: set[Path] = set()
            allowed_roots = (root.resolve(), asset_dir.resolve())
            for icon_path in icon_paths:
                relative_icon = str(icon_path or "").lstrip("/")
                path = (
                    (asset_dir / relative_icon.removeprefix("assets/")).resolve()
                    if relative_icon.startswith("assets/")
                    else (root / relative_icon).resolve()
                )
                try:
                    if not any(path == allowed_root or allowed_root in path.parents for allowed_root in allowed_roots):
                        continue
                    if path in loaded_paths:
                        continue
                    with Image.open(path) as opened:
                        images.append(opened.copy())
                    filenames.append(path.name)
                    loaded_paths.add(path)
                except OSError:
                    continue
            if not images:
                continue
            template_id = normalize_augment_id(
                manifest_item.get("augment_name_id")
                or manifest_item.get("cdragon_id")
                or hint.get("augment_id")
                or clean_name,
                clean_name,
            )
            existing = raw_templates.get(template_id)
            if existing is not None and _clean_text(existing.get("name")) != clean_name:
                template_id = normalize_augment_id(f"{clean_name}_{manifest_item.get('cdragon_id') or ''}", clean_name)
                existing = raw_templates.get(template_id)
            if existing is not None:
                existing["images"] = [*existing.get("images", []), *images]
                existing["source_icon_filenames"] = list(
                    dict.fromkeys([*existing.get("source_icon_filenames", []), *filenames])
                )
                continue
            raw_templates[template_id] = {
                "name": clean_name,
                "tier": _clean_text(manifest_item.get("tier") or hint.get("tier"), fallback="Unknown"),
                "summary": _clean_text(
                    hint.get("summary") or manifest_item.get("tooltip_plain") or manifest_item.get("description"),
                    fallback="本地模板识别结果",
                ),
                "images": images,
                "source_icon_filenames": filenames,
                "priority": 1 if hint_result.get("ok") else 0,
            }
    static_aliases: list[tuple[str, str]] = []
    for item in load_augment_manifest_entries(version_data_dir):
        if not isinstance(item, Mapping):
            continue
        try:
            canonical_id = str(int(item.get("cdragon_id")))
        except (TypeError, ValueError):
            continue
        if int(canonical_id) <= 0:
            continue
        for key in (item.get("augment_name_id"), item.get("name")):
            if key:
                static_aliases.append((str(key), canonical_id))
    return _attach_observed_name_exemplars(
        build_template_index(raw_templates),
        asset_dir,
        hint_cache=hint_cache,
        extra_aliases=static_aliases,
        strict=True,
    )


def publish_template_index(
    raw_entries: Sequence[TemplateEntry],
    matrices: _RankMatrices,
) -> tuple[TemplateIndex, _RankMatrices]:
    """将构建期条目转换为 metadata-only index，并复用同一块连续矩阵。"""

    published = TemplateIndex(entry.without_fingerprints() for entry in raw_entries)
    by_identity = {id(raw): published[index] for index, raw in enumerate(raw_entries)}

    def published_templates(rows: Sequence[TemplateEntry]) -> tuple[TemplateEntry, ...]:
        return tuple(by_identity[id(entry)] for entry in rows)

    published_matrices = _RankMatrices(
        published,
        published_templates(matrices.icon_templates),
        matrices.icon_matrix,
        published_templates(matrices.name_templates),
        matrices.name_matrix,
        published_templates(matrices.alt_name_templates),
        matrices.alt_name_matrix,
        published_templates(matrices.observed_name_templates),
        matrices.observed_name_matrix,
    )
    published.rank_matrices = published_matrices
    return published, published_matrices
