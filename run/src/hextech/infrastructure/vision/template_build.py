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
from hextech.modules.data.ports.paths import ASSET_DIR, INDEX_DATA_DIR
from hextech.modules.recommendation.hints import normalize_augment_id, query_overlay_hint


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
) -> list[TemplateEntry]:
    """绑定脱敏卡名 ROI；去重键基于数组字节，不把指纹转换回 Python tuple。"""

    from hextech.infrastructure.vision.sidecar_fingerprints import _normalized_fingerprint, _text_levels

    exemplar_dir = asset_dir / "vision" / "name_exemplars"
    fingerprints_by_id: dict[str, list[np.ndarray]] = {}
    fingerprints_seen: dict[str, set[bytes]] = {}
    if exemplar_dir.is_dir():
        for path in sorted(exemplar_dir.glob("*.png")):
            augment_id = normalize_augment_id(path.stem.split("__", 1)[0])
            if not augment_id:
                continue
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
    return [
        replace(
            entry,
            observed_name_fingerprints=tuple(fingerprints_by_id.get(normalize_augment_id(entry.augment_id), ())),
        )
        for entry in template_index
    ]


def load_default_template_entries(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> list[TemplateEntry]:
    """从本地 catalog/assets 构建临时模板条目，不触发网络请求。"""

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
    return _attach_observed_name_exemplars(build_template_index(raw_templates), asset_dir)


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
