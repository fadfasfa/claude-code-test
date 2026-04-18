from __future__ import annotations

import json
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import (
    APP_ASSETS_DIR,
    PIPELINE_COLLECT_EXCEL_DIR,
    PIPELINE_STORE_CATALOG_DIR,
    PIPELINE_STORE_RAW_EXCEL_DIR,
    build_weapon_asset_path,
    load_weapon_image_name_overrides,
    resolve_weapon_asset_name,
    sanitize_asset_name,
    weapon_slot_directory,
    write_json,
)

WORKBOOK_FILE = PIPELINE_COLLECT_EXCEL_DIR / "星际战士2数据表.xlsx"
IMPORT_DIR = PIPELINE_STORE_RAW_EXCEL_DIR
WEAPON_IMAGE_MAP_FILE = IMPORT_DIR / "武器图片映射.json"
WEAPON_NAME_LIST_FILE = IMPORT_DIR / "武器名称清单.json"
WEAPON_NAME_MAP_EXPORT_FILE = IMPORT_DIR / "武器名称映射.json"
WEAPON_IMAGE_INDEX_FILE = IMPORT_DIR / "武器图片清单.json"
WEAPON_MANIFEST_FILE = PIPELINE_STORE_CATALOG_DIR / "武器图标清单.json"
EXCEL_EXPORT_FILE = IMPORT_DIR / "Excel原始导出.json"
WEAPON_ICON_ROOT = Path("weapons") / "icons"
DISPIMG_PATTERN = re.compile(r'=DISPIMG\("(?P<id>ID_[A-Z0-9]+)",\s*1\)', re.IGNORECASE)
XML_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "etc": "http://www.wps.cn/officeDocument/2017/etCustomData",
}
SHEET_KEYWORDS = {
    "主武器": {
        "自动爆弹步枪": "auto-bolt-rifle",
        "爆弹卡宾枪": "bolt-carbine",
        "爆弹步枪": "bolt-rifle",
        "爆弹狙击步枪": "bolt-sniper-rifle",
        "多管热熔": "multi-melta",
        "双联热熔枪": "twin-linked-melta-gun",
        "全知者爆弹卡宾枪": "occulus-bolt-carbine",
        "煽动者爆弹卡宾枪": "instigator-bolt-carbine",
        "热熔步枪": "melta-rifle",
        "激光燧发枪": "las-fusil",
        "神射手卡宾枪": "marksman-bolt-carbine",
        "重型爆弹步枪": "heavy-bolt-rifle",
        "重型爆弹枪": "heavy-bolter",
        "重型等离子焚化枪": "heavy-plasma-incinerator",
        "等离子焚化枪": "plasma-incinerator",
        "焚焰枪": "pyreblaster",
        "焚焰炮": "pyrecannon",
        "追猎者爆弹步枪": "stalker-bolt-rifle",
    },
    "副武器": {
        "爆弹手枪": "bolt-pistol",
        "重爆弹手枪": "heavy-bolt-pistol",
        "地狱火手枪": "inferno-pistol",
        "高能爆燃手枪": "neo-volkite-pistol",
        "等离子手枪": "plasma-pistol",
    },
}
WEAPON_TITLE_COLUMN_BY_SHEET = {
    "主武器": 4,
    "副武器": 2,
    "主页-近战武器": 4,
}


@dataclass(frozen=True)
class ImageBinary:
    ext: str
    content: bytes


@dataclass(frozen=True)
class ImportFailure:
    slug: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"slug": self.slug, "reason": self.reason}


@dataclass(frozen=True)
class WeaponBlock:
    display_name: str
    formula: str
    title_fill_rgb: str


CANONICAL_EXCEL_WEAPON_ITEMS: dict[str, dict[str, str]] = {
    "auto-bolt-rifle": {"excel_name": "自动爆弹步枪", "source_sheet": "主武器"},
    "bolt-carbine": {"excel_name": "爆弹卡宾枪", "source_sheet": "主武器"},
    "marksman-bolt-carbine": {"excel_name": "神射手卡宾枪", "source_sheet": "主武器"},
    "bolt-pistol": {"excel_name": "爆弹手枪", "source_sheet": "副武器"},
    "bolt-rifle": {"excel_name": "爆弹步枪", "source_sheet": "主武器"},
    "bolt-sniper-rifle": {"excel_name": "爆弹狙击步枪", "source_sheet": "主武器"},
    "chainsword": {"excel_name": "链锯剑", "source_sheet": "主页-近战武器"},
    "combat-knife": {"excel_name": "匕首", "source_sheet": "主页-近战武器"},
    "heavy-bolt-pistol": {"excel_name": "重爆弹手枪", "source_sheet": "副武器"},
    "heavy-bolt-rifle": {"excel_name": "重型爆弹步枪", "source_sheet": "主武器"},
    "heavy-bolter": {"excel_name": "重型爆弹枪", "source_sheet": "主武器"},
    "heavy-firearms-melee": {"excel_name": "重武器近战", "source_sheet": "主页-近战武器"},
    "heavy-plasma-incinerator": {"excel_name": "重型等离子焚化枪", "source_sheet": "主武器"},
    "inferno-pistol": {"excel_name": "地狱火手枪", "source_sheet": "副武器"},
    "instigator-bolt-carbine": {"excel_name": "煽动者爆弹卡宾枪", "source_sheet": "主武器"},
    "las-fusil": {"excel_name": "激光燧发枪", "source_sheet": "主武器"},
    "melta-rifle": {"excel_name": "热熔步枪", "source_sheet": "主武器"},
    "multi-melta": {"excel_name": "多管热熔", "source_sheet": "主武器"},
    "neo-volkite-pistol": {"excel_name": "高能爆燃手枪", "source_sheet": "副武器"},
    "occulus-bolt-carbine": {"excel_name": "全知者爆弹卡宾枪", "source_sheet": "主武器"},
    "omnissiah-axe": {"excel_name": "欧姆尼赛亚战斧", "source_sheet": "主页-近战武器"},
    "plasma-incinerator": {"excel_name": "等离子焚化枪", "source_sheet": "主武器"},
    "plasma-pistol": {"excel_name": "等离子手枪", "source_sheet": "副武器"},
    "power-axe": {"excel_name": "动力斧", "source_sheet": "主页-近战武器"},
    "power-fist": {"excel_name": "动力拳", "source_sheet": "主页-近战武器"},
    "power-sword": {"excel_name": "动力剑", "source_sheet": "主页-近战武器"},
    "pyreblaster": {"excel_name": "焚焰枪", "source_sheet": "主武器"},
    "pyrecannon": {"excel_name": "焚焰炮", "source_sheet": "主武器"},
    "stalker-bolt-rifle": {"excel_name": "追猎者爆弹步枪", "source_sheet": "主武器"},
    "thunder-hammer": {"excel_name": "雷霆锤", "source_sheet": "主页-近战武器"},
    "twin-linked-melta-gun": {"excel_name": "双联热熔枪", "source_sheet": "主武器"},
}
HERO_TITLE_FILL_RGB = "FF7030A0"
EXCEL_NON_HERO_TITLE_EXCEPTIONS = {"双联热熔枪"}


def _is_excluded_weapon_block_name(display_name: str) -> bool:
    normalized = str(display_name or "").strip()
    return normalized.startswith("英雄")


def _normalize_fill_rgb(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 8 else ""


def _is_hero_weapon_block(*, display_name: str, title_fill_rgb: str) -> bool:
    if str(display_name or "").strip() in EXCEL_NON_HERO_TITLE_EXCEPTIONS:
        return False
    if _normalize_fill_rgb(title_fill_rgb) == HERO_TITLE_FILL_RGB:
        return True
    return _is_excluded_weapon_block_name(display_name)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def _ensure_formula(value: Any) -> str:
    return str(value or "").strip().replace("=_xlfn.", "=")


def _extract_dispimg_id(formula: str) -> str:
    match = DISPIMG_PATTERN.search(_ensure_formula(formula))
    return match.group("id") if match else ""


def _load_workbook_sheet_rows() -> dict[str, list[list[str]]]:
    payload = _read_json(EXCEL_EXPORT_FILE, [])
    sheet_rows: dict[str, list[list[str]]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        sheet_name = str(entry.get("sheet_name", "")).strip()
        rows = entry.get("rows", [])
        if sheet_name and isinstance(rows, list):
            sheet_rows[sheet_name] = rows
    return sheet_rows


def _find_formula_in_export(sheet_rows: dict[str, list[list[str]]], source_sheet: str, excel_name: str) -> str:
    rows = sheet_rows.get(source_sheet, [])
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        for column_index, cell in enumerate(row):
            if str(cell).strip() != excel_name:
                continue
            for neighbor_index in (column_index - 1, column_index + 1):
                if 0 <= neighbor_index < len(row):
                    formula = _ensure_formula(row[neighbor_index])
                    if _extract_dispimg_id(formula):
                        return formula
            for scan_row in (row_index - 1, row_index + 1):
                if 0 <= scan_row < len(rows) and isinstance(rows[scan_row], list):
                    neighbor_row = rows[scan_row]
                    for neighbor_index in (column_index, column_index - 1, column_index + 1):
                        if 0 <= neighbor_index < len(neighbor_row):
                            formula = _ensure_formula(neighbor_row[neighbor_index])
                            if _extract_dispimg_id(formula):
                                return formula
    return ""


def _load_openpyxl_rows(source_sheet: str) -> list[dict[int, str]]:
    workbook = load_workbook(WORKBOOK_FILE, data_only=False)
    target_sheet = None
    for sheet_name in workbook.sheetnames:
        if _normalize_text(sheet_name) == _normalize_text(source_sheet):
            target_sheet = workbook[sheet_name]
            break
    if target_sheet is None:
        return []
    rows: list[dict[int, str]] = []
    for row in target_sheet.iter_rows():
        row_map = {int(cell.column): "" if cell.value is None else str(cell.value) for cell in row}
        if row_map:
            rows.append(row_map)
    return rows


def _collect_weapon_blocks(source_sheet: str) -> list[WeaponBlock]:
    blocks: list[WeaponBlock] = []
    workbook = load_workbook(WORKBOOK_FILE, data_only=False)
    if source_sheet not in workbook.sheetnames:
        return blocks
    worksheet = workbook[source_sheet]
    title_column = WEAPON_TITLE_COLUMN_BY_SHEET.get(source_sheet, 4)
    for row_index in range(1, worksheet.max_row + 1):
        formula = _ensure_formula(worksheet.cell(row=row_index, column=1).value)
        title_cell = worksheet.cell(row=row_index, column=title_column)
        display_name = str(title_cell.value or "").strip()
        title_fill_rgb = _normalize_fill_rgb(title_cell.fill.fgColor.rgb)
        if _extract_dispimg_id(formula) and display_name and not _is_hero_weapon_block(display_name=display_name, title_fill_rgb=title_fill_rgb):
            blocks.append(WeaponBlock(display_name=display_name, formula=formula, title_fill_rgb=title_fill_rgb))
    return blocks


def _build_slug_formula_lookup() -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for source_sheet, keyword_map in SHEET_KEYWORDS.items():
        for block in _collect_weapon_blocks(source_sheet):
            normalized_name = _normalize_text(block.display_name)
            for keyword, slug in keyword_map.items():
                if _normalize_text(keyword) == normalized_name:
                    lookup[slug] = {
                        "formula": block.formula,
                        "source_sheet": source_sheet,
                        "excel_name": block.display_name,
                    }
                    break
    return lookup


def _build_image_rel_lookup() -> dict[str, str]:
    with zipfile.ZipFile(WORKBOOK_FILE) as workbook_zip:
        rels_root = ElementTree.fromstring(workbook_zip.read("xl/_rels/cellimages.xml.rels"))
    lookup: dict[str, str] = {}
    for relation in rels_root:
        relation_id = relation.attrib.get("Id", "")
        target = relation.attrib.get("Target", "")
        if relation_id and target:
            normalized_target = target.removeprefix("/")
            lookup[relation_id] = normalized_target if normalized_target.startswith("xl/") else f"xl/{normalized_target}"
    return lookup


def _build_dispimg_binary_lookup() -> dict[str, ImageBinary]:
    rel_lookup = _build_image_rel_lookup()
    with zipfile.ZipFile(WORKBOOK_FILE) as workbook_zip:
        cellimages_root = ElementTree.fromstring(workbook_zip.read("xl/cellimages.xml"))
        lookup: dict[str, ImageBinary] = {}
        for cell_image in cellimages_root.findall("etc:cellImage", XML_NS):
            name_node = cell_image.find(".//xdr:cNvPr", XML_NS)
            dispimg_id = "" if name_node is None else name_node.attrib.get("name", "")
            blip = cell_image.find(".//a:blip", XML_NS)
            embed_id = "" if blip is None else blip.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed", "")
            target = rel_lookup.get(embed_id, "")
            if not dispimg_id or not target:
                continue
            try:
                content = workbook_zip.read(target)
            except KeyError:
                continue
            lookup[dispimg_id] = ImageBinary(ext=Path(target).suffix.lower() or ".png", content=content)
    return lookup


def _append_missing_manifest_items(items: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    existing_slugs = {str(item.get("slug", "")).strip() for item in items}
    appended = list(items)
    for entry in manifest.get("weapons", []):
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug", "")).strip()
        if not slug or slug in existing_slugs:
            continue
        appended.append({
            "slug": slug,
            "excel_name": "",
            "image_formula": f"__MISSING_MAPPING__:{slug}",
            "source_sheet": "",
        })
    return appended


def _build_manifest_lookup(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(entry.get("slug", "")).strip(): str(entry.get("asset_path", "")).strip()
        for entry in manifest.get("weapons", [])
        if isinstance(entry, dict)
    }


def _infer_sheet_for_slug(slug: str) -> str:
    metadata = CANONICAL_EXCEL_WEAPON_ITEMS.get(str(slug or "").strip(), {})
    return str(metadata.get("source_sheet", "")).strip()


def _slot_type_from_source_sheet(source_sheet: str) -> str:
    normalized = str(source_sheet or "").strip()
    if normalized == "主武器":
        return "primary"
    if normalized == "副武器":
        return "secondary"
    if normalized == "主页-近战武器":
        return "melee"
    return ""


def _find_formula_for_item(item: dict[str, Any], sheet_rows: dict[str, list[list[str]]], slug_formula_lookup: dict[str, dict[str, str]]) -> str:
    current_formula = _ensure_formula(item.get("image_formula", ""))
    lookup_entry = slug_formula_lookup.get(str(item.get("slug", "")).strip(), {})
    lookup_formula = _ensure_formula(lookup_entry.get("formula", ""))
    if _extract_dispimg_id(lookup_formula):
        return lookup_formula
    source_sheet = str(item.get("source_sheet", "")).strip()
    excel_name = str(item.get("excel_name", "")).strip()
    if source_sheet and excel_name:
        formula = _find_formula_in_export(sheet_rows, source_sheet, excel_name)
        if formula:
            return formula
    return current_formula


def _resolve_item_asset_fields(
    *,
    slug: str,
    source_sheet: str,
    excel_name: str,
    default_name: str,
    overrides: dict[str, dict[str, Any]],
) -> tuple[str, str, str]:
    slot_directory = weapon_slot_directory(source_sheet=source_sheet)
    asset_name = resolve_weapon_asset_name(
        slug=slug,
        excel_name=excel_name,
        default_name=default_name,
        overrides=overrides,
    )
    asset_path = build_weapon_asset_path(source_sheet=source_sheet, asset_name=asset_name)
    return slot_directory, asset_name, asset_path


def _materialize_items(
    image_map: dict[str, Any],
    manifest: dict[str, Any],
    sheet_rows: dict[str, list[list[str]]],
    slug_formula_lookup: dict[str, dict[str, str]],
    overrides: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ImportFailure]]:
    base_items = list(image_map.get("items", [])) if isinstance(image_map, dict) else []
    items = _append_missing_manifest_items(base_items, manifest)
    for slug, metadata in CANONICAL_EXCEL_WEAPON_ITEMS.items():
        if slug not in {str(item.get("slug", "")).strip() for item in items}:
            items.append(
                {
                    "slug": slug,
                    "excel_name": metadata["excel_name"],
                    "image_formula": "",
                    "source_sheet": metadata["source_sheet"],
                }
            )
    finalized: list[dict[str, Any]] = []
    failures: list[ImportFailure] = []
    for item in items:
        slug = str(item.get("slug", "")).strip()
        canonical_metadata = CANONICAL_EXCEL_WEAPON_ITEMS.get(slug, {})
        source_sheet = str(canonical_metadata.get("source_sheet", "")).strip() or str(item.get("source_sheet", "")).strip() or _infer_sheet_for_slug(slug)
        excel_name = str(canonical_metadata.get("excel_name", "")).strip() or str(item.get("excel_name", "")).strip()
        formula = _find_formula_for_item({**item, "source_sheet": source_sheet, "excel_name": excel_name}, sheet_rows, slug_formula_lookup)
        default_name = str(item.get("asset_file_name", "")).removesuffix(".png").strip()
        slot_directory, asset_name, asset_path = _resolve_item_asset_fields(
            slug=slug,
            source_sheet=source_sheet,
            excel_name=excel_name,
            default_name=default_name,
            overrides=overrides,
        )
        finalized_item = {
            **item,
            "excel_name": excel_name,
            "source_sheet": source_sheet,
            "image_formula": formula,
            "asset_file_name": f"{sanitize_asset_name(asset_name)}.png",
            "asset_path": asset_path,
            "directory_label": slot_directory,
        }
        finalized.append(finalized_item)
        if not _extract_dispimg_id(formula):
            failures.append(ImportFailure(slug=slug, reason="缺少可解析的 DISPIMG 公式"))
    return finalized, failures


def _write_png(target_path: Path, image_binary: ImageBinary) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(image_binary.content)) as image:
        image.convert("RGBA").save(target_path, format="PNG")


def _cleanup_obsolete_weapon_icons(items: list[dict[str, Any]]) -> None:
    expected_paths = {
        (APP_ASSETS_DIR / Path(str(item.get("asset_path", "")).strip())).resolve()
        for item in items
        if str(item.get("asset_path", "")).strip()
    }
    icon_root = APP_ASSETS_DIR / WEAPON_ICON_ROOT
    if not icon_root.exists():
        return
    for file_path in icon_root.rglob("*.png"):
        if file_path.resolve() not in expected_paths:
            file_path.unlink()


def _import_items(items: list[dict[str, Any]], image_lookup: dict[str, ImageBinary]) -> tuple[list[str], list[ImportFailure]]:
    imported: list[str] = []
    failures: list[ImportFailure] = []
    for item in items:
        slug = str(item.get("slug", "")).strip()
        formula = _ensure_formula(item.get("image_formula", ""))
        dispimg_id = _extract_dispimg_id(formula)
        if not slug:
            continue
        if not dispimg_id:
            continue
        image_binary = image_lookup.get(dispimg_id)
        if image_binary is None:
            failures.append(ImportFailure(slug=slug, reason=f"工作簿中未找到图片 ID: {dispimg_id}"))
            continue
        relative_asset_path = str(item.get("asset_path", "")).strip() or (WEAPON_ICON_ROOT / f"{slug}.png").as_posix()
        _write_png(APP_ASSETS_DIR / Path(relative_asset_path), image_binary)
        imported.append(slug)
    return imported, failures


def _dedupe_failures(failures: list[ImportFailure]) -> list[ImportFailure]:
    unique: dict[tuple[str, str], ImportFailure] = {}
    for failure in failures:
        unique[(failure.slug, failure.reason)] = failure
    return list(unique.values())


def _build_clean_excel_exports(items: list[dict[str, Any]]) -> None:
    normalized_items = [
        {
            "slug": str(item.get("slug", "")).strip(),
            "excel_name": str(item.get("excel_name", "")).strip(),
            "source_sheet": str(item.get("source_sheet", "")).strip(),
            "slot_type": _slot_type_from_source_sheet(str(item.get("source_sheet", "")).strip()),
            "directory_label": str(item.get("directory_label", "")).strip(),
            "asset_file_name": str(item.get("asset_file_name", "")).strip(),
            "asset_path": str(item.get("asset_path", "")).strip(),
        }
        for item in items
        if str(item.get("slug", "")).strip() and str(item.get("excel_name", "")).strip()
    ]

    normalized_items.sort(key=lambda item: (item["slot_type"], item["excel_name"], item["slug"]))

    write_json(
        WEAPON_NAME_MAP_EXPORT_FILE,
        {
            "items": [
                {
                    "slug": item["slug"],
                    "excel_name": item["excel_name"],
                    "slot_type": item["slot_type"],
                    "source_sheet": item["source_sheet"],
                }
                for item in normalized_items
            ],
            "notes": "Excel 规范武器命名。英雄级/变体不会额外生成独立武器分类；运行层统一使用基础武器名。",
        },
    )
    write_json(
        WEAPON_NAME_LIST_FILE,
        {
            "items": [
                {
                    "slug": item["slug"],
                    "excel_name": item["excel_name"],
                    "slot_type": item["slot_type"],
                }
                for item in normalized_items
            ],
            "notes": "去重后的 Excel 武器清单，仅保留运行层使用的基础武器名。",
        },
    )
    write_json(
        WEAPON_IMAGE_INDEX_FILE,
        {
            "items": [
                {
                    "slug": item["slug"],
                    "excel_name": item["excel_name"],
                    "slot_type": item["slot_type"],
                    "source_sheet": item["source_sheet"],
                    "directory_label": item["directory_label"],
                    "asset_file_name": item["asset_file_name"],
                    "asset_path": item["asset_path"],
                }
                for item in normalized_items
            ],
            "notes": "Excel 图像索引。英雄级/词条说明不单列为武器项，图片路径按运行期规范输出。",
        },
    )


def import_weapon_icons() -> dict[str, Any]:
    image_map = _read_json(WEAPON_IMAGE_MAP_FILE, {"items": []})
    manifest = _read_json(WEAPON_MANIFEST_FILE, {"weapons": []})
    sheet_rows = _load_workbook_sheet_rows()
    slug_formula_lookup = _build_slug_formula_lookup()
    image_lookup = _build_dispimg_binary_lookup()
    overrides = load_weapon_image_name_overrides()

    finalized_items, mapping_failures = _materialize_items(image_map, manifest, sheet_rows, slug_formula_lookup, overrides)
    imported_slugs, import_failures = _import_items(finalized_items, image_lookup)
    _cleanup_obsolete_weapon_icons(finalized_items)
    write_json(WEAPON_IMAGE_MAP_FILE, {"items": finalized_items})
    _build_clean_excel_exports(finalized_items)

    failures = _dedupe_failures([*mapping_failures, *import_failures])
    return {
        "workbook": WORKBOOK_FILE.relative_to(PROJECT_ROOT).as_posix(),
        "imported_count": len(imported_slugs),
        "imported_slugs": imported_slugs,
        "failure_count": len(failures),
        "failures": [failure.to_dict() for failure in failures],
        "output_dir": (APP_ASSETS_DIR / WEAPON_ICON_ROOT).relative_to(PROJECT_ROOT).as_posix(),
    }


if __name__ == "__main__":
    print(json.dumps(import_weapon_icons(), ensure_ascii=False, indent=2))
