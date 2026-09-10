"""当前 ARAM: Mayhem 生产识别闭集。

本模块把上游 metadata 的 ``enabled`` 身份与 CDragon Catalog 的视觉资源绑定成
generation 可携带的池。完整 Catalog 仍可用于审计，但任何未进入本池的资源都不
得参与 Vision 生产排名。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from hextech.modules.acquisition.common.icons import normalize_augment_name


POOL_SCHEMA_VERSION = 1
LEGACY_SELECTION_POOL_IDS = frozenset(
    """1001 1002 1004 1005 1006 1007 1011 1013 1015 1018 1019 1020 1022 1025 1026 1027 1028 1029 1030
    1034 1036 1037 1038 1041 1044 1045 1046 1047 1048 1051 1053 1054 1056 1057 1058 1060 1061 1062 1063
    1067 1068 1071 1072 1073 1074 1076 1077 1079 1080 1081 1084 1087 1088 1092 1095 1097 1098 1103 1104
    1105 1112 1113 1115 1116 1118 1129 1133 1134 1136 1138 1141 1149 1150 1151 1152 1154 1156 1170 1180
    1181 1187 1194 1195 1204 1205 1206 1211 1214 1220 1225 1237 1238 1243 1301 1305 1308 1311 1314 1315
    1318 1319 1320 1322 1323 1324 1325 1326 1328 1329 1331 1332 1333 1335 1336 1337 1344 1345 1347 1348
    1349 1353 1356 1358 1361 1373 1375 1379 1384 1386 1388 1389 1390 1392 1401 1402 1403 1404 1413 1415
    1416 1420 1421 1996 2004 2005 2006 2009 2010 2016 2018 2024 2026 2031 2032 2034 2042 2043 2046 2054
    2055 2062 2063 2064 2065 2072 2073 2076 2077 2078 2080 2082 2083 2087 2088 2089 2091 2095 2096 2098
    2099 2100 2102 2103 2104 2107 2111 2115 2116 2118 2119 2123 2125 2127 2128 2129 2131 2132 2133 2134
    2135 2136 2137 2138 2139 2144 12317""".split()
)
LEGACY_PUNCTUATION_NAMES = {
    "1051": "点亮他们!",
    "1237": "质变:黄金阶",
    "1238": "质变:棱彩阶",
    "1243": "质变:混沌",
    "1318": "升级:中娅",
    "1319": "升级:献祭",
    "1320": "升级:收集者",
    "1336": "升级:无尽之刃",
    "1379": "升级:花晓之剑",
    "1402": "属性叠属性叠属性!",
    "1403": "属性叠属性!",
    "1404": "属性!",
    "1996": "升级:耀光",
    "2089": "哎哟,我的硬币!",
    "2111": "邦!",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _id(value: object) -> str:
    text = _text(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _marker_changed(last_good: Mapping[str, Any] | None, current_marker: str) -> bool:
    if not isinstance(last_good, Mapping):
        return True
    previous_marker = _text(last_good.get("metadata_marker_sha256"))
    return bool(previous_marker and current_marker and previous_marker != current_marker)


def _metadata_identity(raw_id: object, raw: Mapping[str, Any]) -> dict[str, Any]:
    canonical_id = _id(raw_id)
    name = _text(raw.get("displayName") or raw.get("name"))
    if not canonical_id or not name:
        raise ValueError("production pool metadata identity 缺少 id/name")
    return {
        "canonical_id": canonical_id,
        "name": name,
        "normalized_name": normalize_augment_name(name),
        "augment_name_id": _text(raw.get("name")),
        "tier": _text(raw.get("rarity")),
        "enabled": bool(raw.get("enabled", True)),
    }


def build_production_augment_pool(
    metadata: Mapping[object, object],
    catalog_entries: Sequence[Mapping[str, Any]],
    *,
    upstream_marker_sha256: str = "",
    catalog_generation_id: str = "",
    catalog_sha256: str = "",
    catalog_manifest_sha256: str = "",
    last_good_pool: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造当前启用身份池；任何 ID 必须有且只有一个 canonical identity。"""

    if not isinstance(metadata, Mapping):
        raise ValueError("production pool metadata 必须是对象")
    catalog_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for entry in catalog_entries:
        if not isinstance(entry, Mapping):
            continue
        source_id = _id(entry.get("source_id") or entry.get("augment_id") or entry.get("cdragon_id"))
        if source_id and source_id != "-1":
            catalog_by_id.setdefault(source_id, []).append(entry)

    identities: list[dict[str, Any]] = []
    disabled_ids: list[str] = []
    unresolved_ids: list[str] = []
    duplicate_ids: list[str] = []
    name_conflicts: list[dict[str, str]] = []
    for raw_id, raw_value in metadata.items():
        raw = raw_value if isinstance(raw_value, Mapping) else {}
        identity = _metadata_identity(raw_id, raw)
        canonical_id = identity["canonical_id"]
        if not identity["enabled"]:
            disabled_ids.append(canonical_id)
            continue
        matches = catalog_by_id.get(canonical_id, [])
        if not matches:
            unresolved_ids.append(canonical_id)
            continue
        # 同一 ID 允许多个资源变体；它们仍属于一个 canonical identity。
        variants: list[dict[str, Any]] = []
        seen_variant_keys: set[tuple[str, str]] = set()
        for entry in matches:
            entry_name = _text(entry.get("name"))
            if entry_name and normalize_augment_name(entry_name) != identity["normalized_name"]:
                name_conflicts.append({"canonical_id": canonical_id, "metadata_name": identity["name"], "catalog_name": entry_name})
            key = (_text(entry.get("augment_name_id") or entry.get("filename")), _text(entry.get("filename")))
            if key in seen_variant_keys:
                continue
            seen_variant_keys.add(key)
            variants.append(
                {
                    "variant_id": _text(entry.get("augment_name_id") or entry.get("filename") or canonical_id),
                    "name": entry_name or identity["name"],
                    "augment_name_id": _text(entry.get("augment_name_id")),
                    "filename": _text(entry.get("filename")),
                    "local_path": _text(entry.get("local_path")),
                    "source_icon_path": _text(entry.get("source_icon_path")),
                    "source_icon_url": _text(entry.get("source_icon_url")),
                    "icon_sha256": _text(entry.get("icon_sha256")),
                }
            )
        identity["visual_variants"] = variants
        identity["icon_ambiguous"] = False
        identities.append(identity)

    identities.sort(key=lambda item: (not str(item["canonical_id"]).isdigit(), int(item["canonical_id"]) if str(item["canonical_id"]).isdigit() else str(item["canonical_id"])))
    canonical_ids = [str(item["canonical_id"]) for item in identities]
    names_to_ids: dict[str, list[str]] = {}
    for item in identities:
        names_to_ids.setdefault(str(item["normalized_name"]), []).append(str(item["canonical_id"]))
    duplicate_ids.extend(
        canonical_id
        for canonical_ids_for_name in names_to_ids.values()
        if len(canonical_ids_for_name) > 1
        for canonical_id in canonical_ids_for_name
    )
    icon_to_ids: dict[str, set[str]] = {}
    for item in identities:
        canonical_id = str(item["canonical_id"])
        for variant in item["visual_variants"]:
            icon_key = _text(variant.get("icon_sha256") or variant.get("local_path"))
            if icon_key:
                icon_to_ids.setdefault(icon_key, set()).add(canonical_id)
    for item in identities:
        item["icon_ambiguous"] = any(
            len(icon_to_ids.get(_text(variant.get("icon_sha256") or variant.get("local_path")), ())) > 1
            for variant in item["visual_variants"]
        )
    previous_ids = {
        _id(value)
        for value in (
            last_good_pool.get("canonical_ids", ())
            if isinstance(last_good_pool, Mapping)
            else LEGACY_SELECTION_POOL_IDS
        )
        if _id(value)
    }
    current_enabled_ids = set(canonical_ids).union(unresolved_ids)
    added_ids = sorted(current_enabled_ids.difference(previous_ids), key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    removed_ids = sorted(previous_ids.difference(current_enabled_ids), key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    marker_changed = _marker_changed(last_good_pool, _text(upstream_marker_sha256))
    change_without_marker = bool(last_good_pool and (added_ids or removed_ids) and not marker_changed)
    renamed = []
    if not isinstance(last_good_pool, Mapping):
        current_names = {str(item["canonical_id"]): str(item["name"]) for item in identities}
        renamed = [
            {"canonical_id": canonical_id, "old_name": old_name, "new_name": current_names[canonical_id]}
            for canonical_id, old_name in LEGACY_PUNCTUATION_NAMES.items()
            if canonical_id in current_names and current_names[canonical_id] != old_name
        ]
    body: dict[str, Any] = {
        "schema_version": POOL_SCHEMA_VERSION,
        "catalog_generation_id": _text(catalog_generation_id),
        "catalog_sha256": _text(catalog_sha256),
        "catalog_manifest_sha256": _text(catalog_manifest_sha256),
        "metadata_marker_sha256": _text(upstream_marker_sha256),
        "metadata_total_count": len(metadata),
        "full_catalog_count": len(catalog_entries),
        "enabled_count": len(identities) + len(unresolved_ids),
        "disabled_count": len(disabled_ids),
        "canonical_ids": canonical_ids,
        "identities": identities,
        "disabled_ids": sorted(disabled_ids),
        "unresolved_ids": sorted(unresolved_ids),
        "duplicate_ids": sorted(set(duplicate_ids)),
        "name_conflicts": name_conflicts[:10],
        "migration": {
            "baseline": "last_good_pool" if isinstance(last_good_pool, Mapping) else "selection_pool_20260730_206",
            "baseline_count": len(previous_ids),
            "added_count": len(added_ids),
            "removed_count": len(removed_ids),
            "added_ids": added_ids,
            "removed_ids": removed_ids,
            "renamed_count": len(renamed),
            "renamed": renamed,
            "marker_changed": marker_changed,
            "change_without_marker": change_without_marker,
        },
    }
    body["pool_id"] = f"production-pool-v1-{_hash_payload(body)[:32]}"
    body["state"] = (
        "ready"
        if not unresolved_ids and not duplicate_ids and not name_conflicts and not change_without_marker
        else "unavailable"
    )
    return body


def validate_production_augment_pool(pool: Mapping[str, Any]) -> None:
    """发布前强制阻断 unresolved/duplicate/name conflict，禁止全量回退。"""

    if str(pool.get("state") or "") != "ready":
        raise ValueError("production_pool_unavailable")
    identities = pool.get("identities")
    canonical_ids = pool.get("canonical_ids")
    if not isinstance(identities, list) or not isinstance(canonical_ids, list):
        raise ValueError("production_pool_unavailable")
    actual = [str(item.get("canonical_id") or "") for item in identities if isinstance(item, Mapping)]
    if len(actual) != len(set(actual)) or actual != [str(value) for value in canonical_ids]:
        raise ValueError("production_pool_duplicate_identity")
    if pool.get("unresolved_ids") or pool.get("duplicate_ids") or pool.get("name_conflicts"):
        raise ValueError("production_pool_unavailable")
    migration = pool.get("migration") if isinstance(pool.get("migration"), Mapping) else {}
    if bool(migration.get("change_without_marker")):
        raise ValueError("production_pool_change_without_marker")


def production_pool_name_ids(pool: Mapping[str, Any]) -> set[str]:
    """返回生产 matcher 可接受的 canonical ID、名称和 variant ID。"""

    result: set[str] = set()
    identities = pool.get("identities") if isinstance(pool, Mapping) else ()
    for item in identities if isinstance(identities, list) else ():
        if not isinstance(item, Mapping):
            continue
        for key in ("canonical_id", "name", "normalized_name", "augment_name_id"):
            value = _text(item.get(key))
            if value:
                result.add(value.casefold())
        variants = item.get("visual_variants")
        for variant in variants if isinstance(variants, list) else ():
            if isinstance(variant, Mapping):
                for key in ("variant_id", "augment_name_id", "filename"):
                    value = _text(variant.get(key))
                    if value:
                        result.add(value.casefold())
    return result


__all__ = [
    "POOL_SCHEMA_VERSION",
    "LEGACY_SELECTION_POOL_IDS",
    "build_production_augment_pool",
    "production_pool_name_ids",
    "validate_production_augment_pool",
]
