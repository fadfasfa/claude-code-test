from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import (
    APP_ASSETS_DIR,
    PIPELINE_STORE_CATALOG_DIR,
    PIPELINE_STORE_REPORTS_SOURCE_DIR,
    PIPELINE_STORE_RAW_WIKI_DIR,
    ensure_directories,
    read_json,
    write_json,
)

CLASS_TITLES = [
    "Tactical",
    "Assault",
    "Vanguard",
    "Bulwark",
    "Sniper",
    "Heavy",
    "Techmarine",
]
CLASS_NAME_MAP = {
    "Tactical": "战术兵",
    "Assault": "突击兵",
    "Vanguard": "先锋兵",
    "Bulwark": "重装兵",
    "Sniper": "狙击兵",
    "Heavy": "特战兵",
    "Techmarine": "技术军士",
}
CLASS_SLUG_MAP = {
    "Tactical": "tactical",
    "Assault": "assault",
    "Vanguard": "vanguard",
    "Bulwark": "bulwark",
    "Sniper": "sniper",
    "Heavy": "heavy",
    "Techmarine": "techmarine",
}

RAW_DATA_FILE = PIPELINE_STORE_RAW_WIKI_DIR / "原始抓取数据.json"
CLASS_MANIFEST_FILE = PIPELINE_STORE_CATALOG_DIR / "职业图片清单.json"
TALENT_MANIFEST_FILE = PIPELINE_STORE_CATALOG_DIR / "按职业分组天赋图标.json"
MANUAL_ACTION_REPORT = PIPELINE_STORE_REPORTS_SOURCE_DIR / "天赋手动补图清单.json"
DOM_DUMP_DIR = PIPELINE_STORE_REPORTS_SOURCE_DIR / "perk_dom_dumps"

TALENT_GRID_COLS = 3
TALENT_GRID_ROWS = 8
TALENT_SLOT_COUNT = TALENT_GRID_COLS * TALENT_GRID_ROWS
TARGET_CLASS_IMAGE_COUNT = 5
WIKI_ORIGIN = "https://spacemarine2.fandom.com"
WIKI_API_URL = f"{WIKI_ORIGIN}/api.php"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_FILENAME_TALENT_RE = re.compile(r"^c(?P<col>\d+)r(?P<row>\d+)_(?P<slug>.+)\.(png|svg)$", re.IGNORECASE)
_WIKI_LINK_WITH_LABEL_RE = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WIKI_FILE_LINK_RE = re.compile(r"\[\[File:[^\]]+\]\]", re.IGNORECASE)
_WIKI_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.IGNORECASE | re.S)
_PERK_TREE_RE = re.compile(r"\{\{PerkTree\|Class\|([^|}]+)", re.IGNORECASE)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def strip_html_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def strip_wiki_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = _WIKI_REF_RE.sub(" ", text)
    text = _WIKI_FILE_LINK_RE.sub(" ", text)
    text = _WIKI_LINK_WITH_LABEL_RE.sub(r"\1", text)
    text = _WIKI_LINK_RE.sub(lambda m: m.group(1).split("|")[-1], text)
    text = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def wiki_api_get_json(params: dict[str, Any], *, retries: int = 4) -> dict[str, Any]:
    last_error = ""
    for _ in range(retries):
        try:
            response = requests.get(WIKI_API_URL, params={**params, "format": "json"}, timeout=30)
            if response.ok:
                return response.json()
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    return {"error": {"info": last_error or "unknown"}}


def fetch_perks_from_wiki_api(class_title: str) -> list[dict[str, Any]]:
    parsed = wiki_api_get_json({"action": "parse", "page": class_title, "prop": "wikitext"})
    wikitext = str(parsed.get("parse", {}).get("wikitext", {}).get("*", "")).strip()
    if not wikitext:
        return []

    slugs: list[str] = []
    seen: set[str] = set()
    for match in _PERK_TREE_RE.finditer(wikitext):
        slug = str(match.group(1) or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        if len(slugs) >= TALENT_SLOT_COUNT:
            break

    perks: list[dict[str, Any]] = []
    for slug in slugs:
        expanded = wiki_api_get_json(
            {
                "action": "expandtemplates",
                "text": f"{{{{PerkTable|Class|{slug}}}}}",
                "prop": "wikitext",
            }
        )
        expanded_text = str(expanded.get("expandtemplates", {}).get("wikitext", "")).strip()
        if not expanded_text:
            continue
        name_raw, _, desc_raw = expanded_text.partition("||")
        english_name = strip_wiki_text(name_raw) or slug.replace("-", " ").title()
        description_plain = strip_wiki_text(desc_raw)
        perks.append(
            {
                "english_name": english_name,
                "icon_url": "",
                "image_key": "",
                "description_raw": str(desc_raw).strip(),
                "description_plain": description_plain,
                "source_section": "api:PerkTable",
                "source_title": class_title,
            }
        )
    return perks


def unique_non_empty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def class_page_url(title: str) -> str:
    return f"{WIKI_ORIGIN}/wiki/{title}"


def normalize_class_name(english_name: str) -> str:
    return CLASS_NAME_MAP.get(english_name, english_name)


def class_slug_for_title(english_name: str) -> str:
    return CLASS_SLUG_MAP.get(english_name, slugify(english_name))


def talent_coords(index: int) -> tuple[int, int]:
    col = (index // TALENT_GRID_ROWS) + 1
    row = (index % TALENT_GRID_ROWS) + 1
    return col, row


def build_grid_label(index: int) -> str:
    col, row = talent_coords(index)
    return f"{col}/{row}"


def clean_placeholder(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text == "/" else text


def clean_talent_name_zh(value: Any, english_name: Any) -> str:
    text = clean_placeholder(value)
    english_text = clean_placeholder(english_name)
    if not text:
        return ""
    if text == english_text and text.isascii():
        return ""
    return text


def normalize_asset_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"{WIKI_ORIGIN}{value}"
    return value


def extract_class_images(page) -> list[str]:
    image_urls = page.evaluate(
        """
        () => {
          const selectors = [
            '.portable-infobox [data-source="image"] img',
            '.portable-infobox img',
            '.pi-item.pi-image img',
            '.mw-parser-output .thumb img',
            '.mw-parser-output img',
            'meta[property="og:image"]',
            'meta[name="twitter:image"]'
          ];
          const urls = [];
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (node.tagName === 'META') {
                urls.push(node.getAttribute('content') || '');
                continue;
              }
              urls.push(node.getAttribute('data-src') || '');
              urls.push(node.getAttribute('src') || '');
            }
          }
          return urls.filter((item) => item && !item.startsWith('data:image'));
        }
        """
    )
    return unique_non_empty([normalize_asset_url(str(item).strip()) for item in image_urls])


def extract_class_summary(page) -> str:
    summary = page.evaluate(
        """
        () => {
          const selectors = [
            '.portable-infobox [data-source="description"]',
            '.mw-parser-output > p',
            '.mw-parser-output p'
          ];
          for (const selector of selectors) {
            const node = document.querySelector(selector);
            if (node && node.textContent.trim()) {
              return node.innerHTML || node.textContent || '';
            }
          }
          return '';
        }
        """
    )
    return str(summary or "").strip()


def extract_perks_from_page(page) -> list[dict[str, Any]]:
    perks = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('.perktree-table .sm2-tooltip')).map((node) => {
          const img = node.querySelector('img');
          const tooltipNode = node.querySelector('.tooltip, .sm2-tooltip__content, [role="tooltip"]');
          const sectionNode = node.closest('section, table, .tabbertab, .perktree-table');
          return {
            english_name: (node.getAttribute('data-title') || node.textContent || '').trim(),
            icon_url: img?.getAttribute('data-src') || img?.getAttribute('src') || '',
            image_key: img?.getAttribute('data-image-key') || '',
            description_raw: tooltipNode?.innerHTML || node.getAttribute('data-description') || '',
            source_section: sectionNode?.getAttribute('data-source') || sectionNode?.getAttribute('id') || sectionNode?.querySelector('h2,h3,h4')?.textContent || '',
            source_title: document.querySelector('h1')?.textContent?.trim() || ''
          };
        }).filter((item) => item.english_name)
        """
    )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in perks:
        english_name = str(item.get("english_name", "")).strip()
        if not english_name or english_name in seen:
            continue
        seen.add(english_name)
        description_raw = str(item.get("description_raw", "")).strip()
        unique.append(
            {
                "english_name": english_name,
                "icon_url": normalize_asset_url(str(item.get("icon_url", "")).strip()),
                "image_key": str(item.get("image_key", "")).strip(),
                "description_raw": description_raw,
                "description_plain": strip_html_text(description_raw),
                "source_section": str(item.get("source_section", "")).strip(),
                "source_title": str(item.get("source_title", "")).strip(),
            }
        )
    return unique[:TALENT_SLOT_COUNT]


def build_manual_action_item(
    class_name: str,
    english_name: str,
    grid_label: str,
    target_asset_rel_path: str,
    source_url: str,
    icon_url: str,
    download_error: str,
) -> dict[str, str]:
    return {
        "class_name": class_name,
        "talent_name_raw": english_name,
        "grid_label_raw": grid_label,
        "target_asset_rel_path": target_asset_rel_path,
        "source_url": source_url,
        "icon_url": icon_url,
        "download_error": download_error,
    }


def download_file(page, url: str, output_path: Path) -> tuple[bool, str, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(1, 4):
        try:
            response = page.request.get(url, timeout=30000)
            if not response.ok:
                last_error = f"HTTP {response.status}"
                continue
            output_path.write_bytes(response.body())
            return True, "", attempt
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    return False, last_error, 3


def write_placeholder(output_path: Path, grid_label: str) -> Path:
    placeholder_path = output_path.with_suffix(".svg")
    placeholder_path.parent.mkdir(parents=True, exist_ok=True)
    placeholder_path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="12" fill="#1c1b1b"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-size="24" fill="#ffb87b">{grid_label}</text></svg>',
        encoding="utf-8",
    )
    return placeholder_path


def list_local_files(asset_dir: Path) -> list[str]:
    if not asset_dir.exists():
        return []
    return sorted(item.name for item in asset_dir.iterdir() if item.is_file())


def cleanup_redundant_talent_svgs(class_name: str) -> None:
    asset_dir = APP_ASSETS_DIR / "talents" / class_name
    if not asset_dir.exists():
        return

    files_by_grid: dict[str, list[Path]] = {}
    for file_path in asset_dir.iterdir():
        if not file_path.is_file():
            continue
        match = _FILENAME_TALENT_RE.match(file_path.name)
        if not match:
            continue
        grid_key = f"{match.group('col')}/{match.group('row')}"
        files_by_grid.setdefault(grid_key, []).append(file_path)

    for file_group in files_by_grid.values():
        has_png = any(item.suffix.lower() == ".png" for item in file_group)
        if not has_png:
            continue
        for file_path in file_group:
            if file_path.suffix.lower() == ".svg":
                file_path.unlink(missing_ok=True)


def cleanup_redundant_class_svgs(class_name: str) -> None:
    asset_dir = APP_ASSETS_DIR / "classes" / class_name
    if not asset_dir.exists():
        return
    cover_png = asset_dir / "cover.png"
    cover_svg = asset_dir / "cover.svg"
    if cover_png.exists() and cover_svg.exists():
        cover_svg.unlink(missing_ok=True)


def remove_unused_svg_assets(*, used_asset_paths: set[str]) -> None:
    for file_path in APP_ASSETS_DIR.rglob("*.svg"):
        relative_path = file_path.relative_to(APP_ASSETS_DIR).as_posix()
        if relative_path not in used_asset_paths:
            file_path.unlink(missing_ok=True)


def choose_cover_file(asset_dir: Path) -> str:
    local_files = list_local_files(asset_dir)
    for candidate in ("cover.png", "cover.svg", "cover.jpg", "cover.jpeg", "cover.webp"):
        if candidate in local_files:
            return candidate
    return local_files[0] if local_files else ""


def build_existing_talent_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog_payload = read_json(TALENT_MANIFEST_FILE, {"classes": []})

    by_class_index: dict[str, dict[str, Any]] = {}
    by_class_name: dict[str, dict[str, Any]] = {}
    for class_entry in catalog_payload.get("classes", []):
        if not isinstance(class_entry, dict):
            continue
        class_slug = str(class_entry.get("class_slug", "")).strip()
        class_name = str(class_entry.get("class_name", "")).strip()
        index_map: dict[str, Any] = {}
        name_map: dict[str, Any] = {}
        for talent in class_entry.get("talents", []):
            if not isinstance(talent, dict):
                continue
            grid_label = str(talent.get("grid_label", "")).strip()
            talent_name = str(talent.get("talent_name", "")).strip()
            if grid_label:
                index_map[grid_label] = talent
            if talent_name:
                name_map[talent_name] = talent
        if class_slug:
            by_class_index[class_slug] = index_map
            by_class_name[class_slug] = name_map

    return by_class_index, by_class_name


def build_talent_entry(
    class_title: str,
    class_name: str,
    class_slug: str,
    perk: dict[str, Any],
    index: int,
    fallback_by_index: dict[str, Any],
    fallback_by_name: dict[str, Any],
) -> dict[str, Any]:
    english_name = str(perk.get("english_name", "")).strip()
    preferred_grid_label = str(perk.get("preferred_grid_label", "")).strip()
    if preferred_grid_label and "/" in preferred_grid_label:
        col_text, row_text = preferred_grid_label.split("/", 1)
        col = int(col_text)
        row = int(row_text)
        grid_label = preferred_grid_label
    else:
        col, row = talent_coords(index)
        grid_label = f"{col}/{row}"
    fallback: dict[str, Any] = {}
    if english_name:
        fallback = fallback_by_name.get(english_name, {})
    if not fallback and not preferred_grid_label:
        fallback = fallback_by_index.get(grid_label, {})

    talent_slug = slugify(english_name) or str(fallback.get("talent_slug", "")).strip() or f"talent-{index + 1}"
    talent_name_zh = clean_talent_name_zh(fallback.get("talent_name_zh", ""), english_name)
    fallback_name = str(fallback.get("talent_name", "")).strip()
    description_raw = clean_placeholder(perk.get("description_raw", ""))
    description_plain = clean_placeholder(perk.get("description_plain", ""))
    icon_url = str(perk.get("icon_url", "")).strip() or str(fallback.get("icon_url", "")).strip()
    return {
        "class_slug": class_slug,
        "class_name": class_name,
        "talent_name_raw": english_name,
        "talent_name_en": english_name,
        "talent_name_zh": talent_name_zh,
        "talent_slug": talent_slug,
        "talent_name": talent_name_zh or fallback_name or english_name or talent_slug,
        "col": col,
        "row": row,
        "grid_index": index,
        "grid_label": grid_label,
        "icon_url": icon_url,
        "image_key": str(perk.get("image_key", "")).strip(),
        "icon_path": f"talents/{class_name}/c{col}r{row}_{talent_slug}.png",
        "source_page": class_page_url(class_title),
        "source_section": str(perk.get("source_section", "")).strip(),
        "source_title": str(perk.get("source_title", "")).strip(),
        "download_status": "pending",
        "download_attempts": 0,
        "description": description_plain or "",
        "description_raw": description_raw or description_plain or "",
    }


def reuse_or_download_talent(
    page,
    entry: dict[str, Any],
    *,
    force_download: bool,
    manual_actions: list[dict[str, str]],
) -> dict[str, Any]:
    output_path = APP_ASSETS_DIR / str(entry["icon_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not force_download and output_path.exists():
        entry["download_status"] = "reused-local"
        entry["download_attempts"] = 0
        return entry

    existing_svg = output_path.with_suffix(".svg")
    if not force_download and existing_svg.exists():
        entry["icon_path"] = existing_svg.relative_to(APP_ASSETS_DIR).as_posix()
        entry["download_status"] = "reused-local"
        entry["download_attempts"] = 0
        return entry

    icon_url = str(entry.get("icon_url", "")).strip()
    if icon_url:
        ok, error_text, attempts = download_file(page, icon_url, output_path)
        entry["download_attempts"] = attempts
        if ok:
            entry["download_status"] = "ok"
            entry["icon_path"] = output_path.relative_to(APP_ASSETS_DIR).as_posix()
            return entry
    else:
        error_text = "No talent icon found on wiki page"
        attempts = 0

    placeholder_path = write_placeholder(output_path, str(entry.get("grid_label", "?")))
    entry["icon_path"] = placeholder_path.relative_to(APP_ASSETS_DIR).as_posix()
    entry["download_status"] = "failed-hard"
    entry["manual_action_required"] = True
    entry["download_attempts"] = attempts
    entry["download_error"] = error_text
    manual_actions.append(
        build_manual_action_item(
            str(entry["class_name"]),
            str(entry["talent_name_raw"]),
            str(entry["grid_label"]),
            entry["icon_path"],
            str(entry["source_page"]),
            icon_url,
            error_text,
        )
    )
    return entry


def ensure_class_cover(
    page,
    *,
    class_title: str,
    class_name: str,
    class_image_url: str,
    force_download: bool,
    manual_actions: list[dict[str, str]],
) -> tuple[str, list[str]]:
    asset_dir = APP_ASSETS_DIR / "classes" / class_name
    asset_dir.mkdir(parents=True, exist_ok=True)
    cleanup_redundant_class_svgs(class_name)
    existing_cover = choose_cover_file(asset_dir)
    if existing_cover and existing_cover.lower().endswith(".png") and not force_download:
        local_images = list_local_files(asset_dir)
        return existing_cover, local_images

    output_path = asset_dir / "cover.png"
    if class_image_url:
        ok, error_text, _ = download_file(page, class_image_url, output_path)
        if ok:
            local_images = list_local_files(asset_dir)
            return "cover.png", local_images
    else:
        error_text = "No class image found on wiki page"

    placeholder_path = write_placeholder(asset_dir / "cover.png", "cover")
    cleanup_redundant_class_svgs(class_name)
    local_images = list_local_files(asset_dir)
    manual_actions.append(
        build_manual_action_item(
            class_name,
            "[CLASS_IMAGE]",
            "cover",
            placeholder_path.relative_to(APP_ASSETS_DIR).as_posix(),
            class_page_url(class_title),
            class_image_url,
            error_text,
        )
    )
    return placeholder_path.name, local_images


def synthesize_perks_from_local(class_name: str, fallback_by_index: dict[str, Any]) -> list[dict[str, Any]]:
    cleanup_redundant_talent_svgs(class_name)
    asset_dir = APP_ASSETS_DIR / "talents" / class_name
    items_by_grid: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for file_name in list_local_files(asset_dir):
        match = _FILENAME_TALENT_RE.match(file_name)
        if not match:
            continue
        col = int(match.group("col"))
        row = int(match.group("row"))
        grid_label = f"{col}/{row}"
        fallback = fallback_by_index.get(grid_label, {})
        english_name = match.group("slug").replace("-", " ").title()
        sort_key = ((col - 1) * TALENT_GRID_ROWS) + row
        extension_score = 0 if file_name.lower().endswith(".png") else 1
        candidate = (
            sort_key,
            extension_score,
            {
                "english_name": english_name,
                "icon_url": "",
                "image_key": "",
                "description_raw": "",
                "description_plain": "",
                "source_section": str(fallback.get("source_section", "")).strip(),
                "source_title": str(fallback.get("source_title", "")).strip(),
                "preferred_grid_label": grid_label,
            },
        )
        current = items_by_grid.get(grid_label)
        if current is None or candidate[1] < current[1]:
            items_by_grid[grid_label] = candidate
    return [item for _, _, item in sorted(items_by_grid.values(), key=lambda pair: pair[0])][:TALENT_SLOT_COUNT]


def page_perk_lookup(perks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        slugify(str(item.get("english_name", "")).strip()): item
        for item in perks
        if isinstance(item, dict) and slugify(str(item.get("english_name", "")).strip())
    }


def scrape_class(
    page,
    class_title: str,
    *,
    force_download: bool,
    fallback_by_index: dict[str, Any],
    fallback_by_name: dict[str, Any],
    image_key: str,
    raw_class_image_url: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    page.goto(class_page_url(class_title), wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)

    class_name = normalize_class_name(class_title)
    class_slug = class_slug_for_title(class_title)
    class_image_candidates = unique_non_empty([*extract_class_images(page), raw_class_image_url])
    class_image_url = class_image_candidates[0] if class_image_candidates else ""
    class_summary_raw = extract_class_summary(page)
    manual_actions: list[dict[str, str]] = []

    default_cover, local_images = ensure_class_cover(
        page,
        class_title=class_title,
        class_name=class_name,
        class_image_url=class_image_url,
        force_download=force_download,
        manual_actions=manual_actions,
    )

    page_perks = extract_perks_from_page(page)
    api_perks = fetch_perks_from_wiki_api(class_title)
    perks = page_perks if len(page_perks) >= TALENT_SLOT_COUNT else api_perks
    local_perks = synthesize_perks_from_local(class_name, fallback_by_index)
    if len(local_perks) >= TALENT_SLOT_COUNT and not force_download:
        perks = local_perks[:TALENT_SLOT_COUNT]
        page_lookup = page_perk_lookup(page_perks or api_perks)
        for index, local_perk in enumerate(perks):
            matched_page_perk = page_lookup.get(slugify(str(local_perk.get("english_name", "")).strip()))
            if not matched_page_perk:
                continue
            perks[index] = {
                **local_perk,
                **matched_page_perk,
                "preferred_grid_label": local_perk.get("preferred_grid_label", ""),
            }
    elif not perks:
        perks = local_perks
    talents: list[dict[str, Any]] = []
    for index, perk in enumerate(perks):
        talent = build_talent_entry(
            class_title,
            class_name,
            class_slug,
            perk,
            index,
            fallback_by_index,
            fallback_by_name,
        )
        talents.append(reuse_or_download_talent(page, talent, force_download=force_download, manual_actions=manual_actions))

    class_payload = {
        "class_name": class_name,
        "class_slug_candidate": class_slug,
        "source_url": class_page_url(class_title),
        "class_image_url": class_image_url,
        "class_image_candidates": class_image_candidates,
        "class_image_asset_rel_path": f"classes/{class_name}/{default_cover}" if default_cover else "",
        "class_summary_raw": class_summary_raw,
        "class_summary_plain": strip_html_text(class_summary_raw),
        "talents": talents,
        "manual_action_required": bool(manual_actions),
    }

    class_manifest = {
        "slug": class_slug,
        "image_key": image_key or f"class_{class_slug}_img",
        "asset_dir": f"classes/{class_name}",
        "local_images": local_images,
        "default_image": default_cover,
        "target_count": TARGET_CLASS_IMAGE_COUNT,
        "source_page": class_page_url(class_title),
    }

    talent_manifest = {
        "class_slug": class_slug,
        "class_name": class_name,
        "asset_dir": f"talents/{class_name}",
        "grid_spec": {
            "cols": TALENT_GRID_COLS,
            "rows": TALENT_GRID_ROWS,
            "label_format": "col/row",
            "order": "column-major",
        },
        "talents": [
            {
                "class_slug": class_slug,
                "class_name": class_name,
                "talent_slug": talent["talent_slug"],
                "talent_name": talent["talent_name"],
                "talent_name_en": talent["talent_name_en"],
                "talent_name_zh": talent["talent_name_zh"],
                "icon_path": talent["icon_path"],
                "icon_url": talent["icon_url"],
                "col": talent["col"],
                "row": talent["row"],
                "grid_label": talent["grid_label"],
                "description": talent["description"],
                "description_raw": talent["description_raw"],
                "source_page": talent["source_page"],
                "source_section": talent["source_section"],
                "source_title": talent["source_title"],
            }
            for talent in talents
        ],
    }
    return class_payload, class_manifest, talent_manifest, manual_actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量抓取 SM2 各职业 perk 图标并刷新 catalog manifest")
    parser.add_argument("--class", dest="class_titles", action="append", default=[], help="指定职业英文页名，可重复传入，如 --class Tactical")
    parser.add_argument("--headless", action="store_true", help="以无头模式运行")
    parser.add_argument("--dump-dom", action="store_true", help="为每个职业落盘 HTML 片段，便于后续维护")
    parser.add_argument("--force-download", action="store_true", help="忽略现有本地素材，强制重新下载职业图与天赋图标")
    return parser.parse_args()


def selected_classes(args: argparse.Namespace) -> list[str]:
    return args.class_titles or CLASS_TITLES


def write_manual_action_report(items: list[dict[str, str]]) -> None:
    write_json(MANUAL_ACTION_REPORT, {"items": items})


def merge_by_key(existing: list[dict[str, Any]], updates: list[dict[str, Any]], key: str, order: list[str]) -> list[dict[str, Any]]:
    by_key = {
        str(item.get(key, "")).strip(): item
        for item in existing
        if isinstance(item, dict) and str(item.get(key, "")).strip()
    }
    for item in updates:
        item_key = str(item.get(key, "")).strip()
        if item_key:
            by_key[item_key] = item
    ordered: list[dict[str, Any]] = []
    for item_key in order:
        if item_key in by_key:
            ordered.append(by_key.pop(item_key))
    ordered.extend(by_key.values())
    return ordered


def load_class_image_meta(raw_payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for class_entry in raw_payload.get("classes", []):
        if not isinstance(class_entry, dict):
            continue
        slug = str(class_entry.get("slug_candidate", "")).strip()
        image_key = str(class_entry.get("image_key", "")).strip()
        image_url = normalize_asset_url(str(class_entry.get("image_url", "")).strip())
        if slug:
            result[slug] = {"image_key": image_key, "image_url": image_url}
    return result


def main() -> int:
    args = parse_args()
    ensure_directories()
    DOM_DUMP_DIR.mkdir(parents=True, exist_ok=True)

    raw_payload = read_json(RAW_DATA_FILE, {})
    class_image_meta = load_class_image_meta(raw_payload)
    existing_talents = raw_payload.get("talents", []) if isinstance(raw_payload.get("talents"), list) else []
    existing_class_manifest = read_json(CLASS_MANIFEST_FILE, {"classes": []})
    existing_talent_manifest = read_json(TALENT_MANIFEST_FILE, {"classes": []})
    fallback_by_index_map, fallback_by_name_map = build_existing_talent_maps()

    requested_classes = selected_classes(args)
    requested_slugs = [class_slug_for_title(title) for title in requested_classes]
    ordered_slugs = [class_slug_for_title(title) for title in CLASS_TITLES]
    talent_classes: list[dict[str, Any]] = []
    class_manifest_updates: list[dict[str, Any]] = []
    talent_manifest_updates: list[dict[str, Any]] = []
    all_manual_actions: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        for class_title in requested_classes:
            class_slug = class_slug_for_title(class_title)
            page = browser.new_page()
            try:
                class_payload, class_manifest, talent_manifest, manual_actions = scrape_class(
                    page,
                    class_title,
                    force_download=args.force_download,
                    fallback_by_index=fallback_by_index_map.get(class_slug, {}),
                    fallback_by_name=fallback_by_name_map.get(class_slug, {}),
                    image_key=class_image_meta.get(class_slug, {}).get("image_key", ""),
                    raw_class_image_url=class_image_meta.get(class_slug, {}).get("image_url", ""),
                )
                if args.dump_dom:
                    dump_path = DOM_DUMP_DIR / f"{class_title.lower()}_perk_page.html"
                    dump_path.write_text(page.content(), encoding="utf-8")
                talent_classes.append(class_payload)
                class_manifest_updates.append(class_manifest)
                talent_manifest_updates.append(talent_manifest)
                all_manual_actions.extend(manual_actions)
            finally:
                page.close()
        browser.close()
    raw_payload["talents"] = merge_by_key(existing_talents, talent_classes, "class_slug_candidate", ordered_slugs)
    raw_meta = dict(raw_payload.get("meta", {}))
    raw_meta["talent_manual_action_items"] = all_manual_actions
    raw_payload["meta"] = raw_meta

    class_manifest_payload = {
        "classes": merge_by_key(
            existing_class_manifest.get("classes", []) if isinstance(existing_class_manifest, dict) else [],
            class_manifest_updates,
            "slug",
            ordered_slugs,
        )
    }
    talent_manifest_payload = {
        "classes": merge_by_key(
            existing_talent_manifest.get("classes", []) if isinstance(existing_talent_manifest, dict) else [],
            talent_manifest_updates,
            "class_slug",
            ordered_slugs,
        )
    }

    write_json(RAW_DATA_FILE, raw_payload)
    write_json(CLASS_MANIFEST_FILE, class_manifest_payload)
    write_json(TALENT_MANIFEST_FILE, talent_manifest_payload)
    used_asset_paths = {
        talent["icon_path"]
        for class_entry in talent_manifest_payload["classes"]
        for talent in class_entry.get("talents", [])
        if isinstance(talent, dict) and str(talent.get("icon_path", "")).strip()
    }
    used_asset_paths.update(
        f"{class_entry['asset_dir']}/{image_name}"
        for class_entry in class_manifest_payload["classes"]
        if isinstance(class_entry, dict)
        for image_name in class_entry.get("local_images", [])
        if str(class_entry.get("asset_dir", "")).strip() and str(image_name or "").strip()
    )
    remove_unused_svg_assets(used_asset_paths=used_asset_paths)
    write_manual_action_report(all_manual_actions)

    if all_manual_actions:
        print("[TALENT-MANUAL-ACTION] 以下图片三次尝试后仍失败，请手动复制：")
        for item in all_manual_actions:
            print(f"- {item['class_name']} | {item['talent_name_raw']} | {item['grid_label_raw']} | {item['target_asset_rel_path']}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
