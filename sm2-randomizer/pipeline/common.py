from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
APP_DATA_DIR = APP_DIR / "data"
APP_ASSETS_DIR = APP_DIR / "assets"
DEBUG_DIR = BASE_DIR / "debug"
DOCS_DIR = BASE_DIR / "docs"

PIPELINE_DIR = BASE_DIR / "pipeline"
PIPELINE_COLLECT_DIR = PIPELINE_DIR / "collect"
PIPELINE_COLLECT_WIKI_DIR = PIPELINE_COLLECT_DIR / "wiki"
PIPELINE_COLLECT_EXCEL_DIR = PIPELINE_COLLECT_DIR / "excel"
PIPELINE_COLLECT_MANUAL_DIR = PIPELINE_COLLECT_DIR / "manual"
PIPELINE_COLLECT_RULES_DIR = PIPELINE_COLLECT_DIR / "rules"

PIPELINE_COMPUTE_DIR = PIPELINE_DIR / "compute"
PIPELINE_TMP_PUBLISH_DIR = PIPELINE_DIR / "tmp_publish"

PIPELINE_STORE_DIR = PIPELINE_DIR / "store"
PIPELINE_STORE_RAW_DIR = PIPELINE_STORE_DIR / "raw"
PIPELINE_STORE_RAW_WIKI_DIR = PIPELINE_STORE_RAW_DIR / "wiki"
PIPELINE_STORE_RAW_EXCEL_DIR = PIPELINE_STORE_RAW_DIR / "excel"
PIPELINE_STORE_CATALOG_DIR = PIPELINE_STORE_DIR / "catalog"
PIPELINE_STORE_REPORTS_DIR = PIPELINE_STORE_DIR / "reports"
PIPELINE_STORE_REPORTS_SOURCE_DIR = PIPELINE_STORE_REPORTS_DIR / "source"
PIPELINE_STORE_REPORTS_RUNTIME_DIR = PIPELINE_STORE_REPORTS_DIR / "runtime"

MANUAL_SOURCE_FILE = PIPELINE_COLLECT_MANUAL_DIR / "手工补录数据.json"
WEAPON_IMAGE_NAME_OVERRIDES_FILE = PIPELINE_COLLECT_MANUAL_DIR / "weapon_image_name_overrides.json"
TALENT_NAME_ZH_FILE = PIPELINE_COLLECT_MANUAL_DIR / "talent_name_zh.json"
TALENT_DESCRIPTION_ZH_FILE = PIPELINE_COLLECT_MANUAL_DIR / "talent_description_zh.json"
WIKI_RAW_FILE = PIPELINE_STORE_RAW_WIKI_DIR / "原始抓取数据.json"
FIELD_SOURCE_POLICY_FILE = PIPELINE_COLLECT_RULES_DIR / "field_source_policy.json"
EXTRACTION_RULES_FILE = PIPELINE_COLLECT_RULES_DIR / "extraction_rules.json"
VALIDATION_REPORT_FILE = PIPELINE_STORE_REPORTS_RUNTIME_DIR / "runtime_validation.json"


def ensure_directories() -> None:
    for path in (
        APP_DIR,
        APP_DATA_DIR,
        APP_ASSETS_DIR,
        DEBUG_DIR,
        DOCS_DIR,
        PIPELINE_DIR,
        PIPELINE_COLLECT_DIR,
        PIPELINE_COLLECT_WIKI_DIR,
        PIPELINE_COLLECT_EXCEL_DIR,
        PIPELINE_COLLECT_MANUAL_DIR,
        PIPELINE_COLLECT_RULES_DIR,
        PIPELINE_COMPUTE_DIR,
        PIPELINE_TMP_PUBLISH_DIR,
        PIPELINE_STORE_DIR,
        PIPELINE_STORE_RAW_DIR,
        PIPELINE_STORE_RAW_WIKI_DIR,
        PIPELINE_STORE_RAW_EXCEL_DIR,
        PIPELINE_STORE_CATALOG_DIR,
        PIPELINE_STORE_REPORTS_DIR,
        PIPELINE_STORE_REPORTS_SOURCE_DIR,
        PIPELINE_STORE_REPORTS_RUNTIME_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "-".join(part for part in "".join(ch if ch.isalnum() else "-" for ch in text).split("-") if part)


def relative_asset_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("/")


def sanitize_asset_name(value: Any) -> str:
    text = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        text = text.replace(char, "-")
    return " ".join(text.split())


def weapon_slot_directory(*, slot_type: str = "", source_sheet: str = "") -> str:
    normalized_slot = str(slot_type or "").strip().lower()
    normalized_sheet = str(source_sheet or "").strip()
    if normalized_slot == "primary" or normalized_sheet == "主武器":
        return "主武器"
    if normalized_slot == "secondary" or normalized_sheet == "副武器":
        return "副手"
    if normalized_slot == "melee" or normalized_sheet == "主页-近战武器":
        return "近战"
    return "主武器"


def load_weapon_image_name_overrides() -> dict[str, dict[str, Any]]:
    payload = read_json(WEAPON_IMAGE_NAME_OVERRIDES_FILE, {"items": []})
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return {
        str(item.get("slug", "")).strip(): item
        for item in items
        if isinstance(item, dict) and str(item.get("slug", "")).strip()
    }


def resolve_weapon_asset_name(
    *,
    slug: str,
    excel_name: str = "",
    default_name: str = "",
    overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    effective_overrides = overrides or {}
    override = effective_overrides.get(str(slug or "").strip(), {})
    preferred_name = str(override.get("preferred_asset_name", "")).strip()
    if preferred_name:
        return sanitize_asset_name(preferred_name)
    if str(excel_name or "").strip():
        return sanitize_asset_name(excel_name)
    if str(default_name or "").strip():
        return sanitize_asset_name(default_name)
    return sanitize_asset_name(slug)


def build_weapon_asset_path(
    *,
    slot_type: str = "",
    source_sheet: str = "",
    asset_name: str,
) -> str:
    directory = weapon_slot_directory(slot_type=slot_type, source_sheet=source_sheet)
    return f"weapons/icons/{directory}/{sanitize_asset_name(asset_name)}.png"
