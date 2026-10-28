"""腾讯官方 KIWI 名单：身份权威与可选资源严格分开。"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

OFFICIAL_AUGMENTS_URL = "https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json"
OFFICIAL_POLICY_REVISION = "tencent-kiwi-non-pbe-v1"
TIERS = {"kBronze": "白银", "kSilver": "白银", "kGold": "黄金", "kPrismatic": "棱彩"}


def normalize_official_augments(payload: object) -> dict[str, dict[str, Any]]:
    """Only exact mode membership admits an identity; isNew is not eligibility."""
    if not isinstance(payload, list) or not payload:
        raise ValueError("official_roster_empty_or_invalid")
    result: dict[str, dict[str, Any]] = {}
    seen: set[int] = set()
    for row in payload:
        if not isinstance(row, Mapping):
            raise ValueError("official_roster_row_invalid")
        identity, pbe = row.get("augmentID"), row.get("isPBE")
        if type(identity) is not int or identity <= 0 or identity in seen:
            raise ValueError("official_roster_id_invalid_or_duplicate")
        seen.add(identity)
        if type(pbe) is not int or pbe not in (0, 1) or not isinstance(row.get("mode"), str):
            raise ValueError("official_roster_eligibility_invalid")
        modes = {part.strip() for part in row["mode"].split(",")}
        if pbe or "KIWI" not in modes:
            continue
        name, internal, rarity = (row.get(key) for key in ("name_cn", "name_en", "level"))
        if not all(isinstance(value, str) and value.strip() for value in (name, internal, rarity)) or rarity not in TIERS:
            raise ValueError("official_roster_identity_invalid")
        icon = row.get("small_Icon", "")
        # Metadata may lose an asset without losing its verified identity.
        try:
            parsed = urlsplit(icon) if isinstance(icon, str) else None
            valid_icon = bool(parsed and parsed.scheme == "https" and parsed.hostname == "game.gtimg.cn"
                              and parsed.port in (None, 443) and not parsed.username and not parsed.password)
        except ValueError:
            valid_icon = False
        result[str(identity)] = {
            "enabled": True, "displayName": " ".join(name.split()), "name": internal.strip(),
            "rarity": rarity, "icon_url": icon if valid_icon else "",
        }
    if not result:
        raise ValueError("official_roster_has_no_live_kiwi_identities")
    return dict(sorted(result.items(), key=lambda item: int(item[0])))


def bind_official_entries(catalog: dict[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Official identity wins; a conflicting resource never supplies an icon."""
    entries = {str(item["cdragon_id"]): dict(item) for item in catalog["entries"]
               if str(item.get("cdragon_id", "")) != "-1"}
    placeholders = [dict(item) for item in catalog["entries"] if item.get("cdragon_id") == -1]
    for identity, raw in metadata.items():
        if not raw.get("enabled"):
            continue
        previous = entries.get(str(identity), {})
        official_name, internal = str(raw.get("displayName") or ""), str(raw.get("name") or "")
        conflict = bool(previous.get("augment_name_id") and previous["augment_name_id"] != internal)
        icon = str(raw.get("icon_url") or "")
        if not icon and not conflict:
            icon = str(previous.get("source_icon_url") or "")
        entry = {**previous, "schema_version": 2, "cdragon_id": int(identity),
                 "name": official_name, "augment_name_id": internal,
                 "tier": TIERS.get(str(raw.get("rarity") or ""), str(raw.get("rarity") or "")),
                 "source_name": str(previous.get("name") or ""),
                 "source_schema": OFFICIAL_POLICY_REVISION,
                 "source_icon_url": icon, "source_icon_path": icon,
                 "filename": PurePosixPath(urlsplit(icon).path).name if icon else "",
                 "local_path": "", "icon_url": ""}
        entry.pop("icon_sha256", None)
        if conflict:
            entry["resource_identity_conflict"] = True
        entries[str(identity)] = entry
    return {**catalog, "description": "官方身份目录与已知资源审计集合；生产池仅采用官方正式 KIWI 名单。",
            "entries": sorted([*entries.values(), *placeholders], key=lambda item: (item["name"], str(item["cdragon_id"])))}
