from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import PIPELINE_STORE_CATALOG_DIR, PIPELINE_STORE_RAW_EXCEL_DIR, PIPELINE_STORE_RAW_WIKI_DIR, PIPELINE_STORE_REPORTS_SOURCE_DIR, ensure_directories, read_json, write_json

VERSION_ANCHOR = "Update 12.0 / Techmarine"
RAW_OUTPUT_PATH = PIPELINE_STORE_RAW_WIKI_DIR / "原始抓取数据.json"
CLASS_WEAPON_MAP_OUTPUT_PATH = PIPELINE_STORE_CATALOG_DIR / "职业武器映射.json"
TABLE_OUTPUT_PATH = PIPELINE_STORE_REPORTS_SOURCE_DIR / "人工审阅表.md"
TALENT_IMPORT_PATH = PIPELINE_STORE_RAW_EXCEL_DIR / "天赋技能效果.json"
TALENT_GRID_COLS = 3
TALENT_GRID_ROWS = 8
TALENT_SLOT_COUNT = TALENT_GRID_COLS * TALENT_GRID_ROWS
CLASS_NAME_MAP = {
    "Tactical": "战术兵",
    "Assault": "突击兵",
    "Vanguard": "先锋兵",
    "Bulwark": "重装兵",
    "Sniper": "狙击兵",
    "Heavy": "特战兵",
    "Techmarine": "技术军士",
}
CLASS_TITLES = ["Tactical", "Assault", "Vanguard", "Bulwark", "Sniper", "Heavy", "Techmarine"]
SLOT_LABELS = [
    {"fandom": "Primary Weapons", "key": "primary", "slot_type": "primary"},
    {"fandom": "Secondary Weapons", "key": "secondary", "slot_type": "secondary"},
    {"fandom": "Melee Weapons", "key": "melee", "slot_type": "melee"},
]
GAME8_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
HTTP_TIMEOUT = 30
CONFLICT_PREFIX = "[CONFLICT]"


def slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def uniq(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def conflict_note(message: str) -> str:
    return f"{CONFLICT_PREFIX} {normalize_whitespace(message)}"


def create_parse_metadata(source_type: str, source_pages: list[str]) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_pages": uniq(source_pages),
        "parse_warnings": [],
        "parse_degraded": False,
        "missing_fields": [],
    }


def finalize_parse_metadata(record: dict[str, Any], required_fields: list[str]) -> dict[str, Any]:
    missing_fields: list[str] = []
    for field in required_fields:
        value = record.get(field)
        if isinstance(value, list):
            if not value:
                missing_fields.append(field)
        elif not value:
            missing_fields.append(field)
    record["missing_fields"] = uniq([*record.get("missing_fields", []), *missing_fields])
    if record["missing_fields"]:
        record["parse_degraded"] = True
        record["parse_warnings"] = uniq([
            *record.get("parse_warnings", []),
            f"Missing critical fields: {', '.join(record['missing_fields'])}",
        ])
    return record


def build_review_record(entity_type: str, display_name: str, reason: str, source_pages: list[str]) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "display_name": display_name,
        "reason": reason,
        "source_pages": uniq(source_pages),
    }


def derive_mode_restriction_candidates(notes: list[str]) -> list[str]:
    return uniq([note for note in notes if re.search(r"\bPvE\b|\bPvP\b", str(note), re.I)])


def get_json(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers=headers or GAME8_HEADERS, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_html(url: str, *, headers: dict[str, str] | None = None) -> str:
    response = requests.get(url, headers=headers or GAME8_HEADERS, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.text


def fetch_official_patch_anchor() -> dict[str, str]:
    url = "https://community.focus-entmt.com/focus-entertainment/space-marine-2/blogs/356-patch-notes-12-0"
    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")
    title = normalize_whitespace(soup.title.get_text() if soup.title else "")
    body_text = normalize_whitespace(soup.get_text(" "))
    if not re.search(r"Patch Notes 12\.0", title, re.I) or not re.search(r"Techmarine", body_text, re.I):
        raise RuntimeError("Official Update 12.0 anchor did not confirm Techmarine availability.")
    return {"url": url, "title": title}


def fetch_fandom_page(title: str) -> dict[str, Any]:
    payload = get_json(
        "https://spacemarine2.fandom.com/api.php",
        params={"action": "parse", "page": title, "prop": "text", "format": "json", "origin": "*"},
    )
    parse = payload.get("parse") or {}
    html = ((parse.get("text") or {}).get("*")) if isinstance(parse.get("text"), dict) else None
    if not parse or not html:
        raise RuntimeError(f"Failed to fetch Fandom page: {title}")
    return {
        "title": title,
        "pageId": parse.get("pageid"),
        "url": f"https://spacemarine2.fandom.com/wiki/{quote(title.replace(' ', '_'))}",
        "html": html,
    }


def find_infobox_value(soup: BeautifulSoup, source_name: str):
    return soup.select_one(f'.pi-data[data-source="{source_name}"] .pi-data-value')


def extract_image_url(soup: BeautifulSoup) -> str:
    candidates = [
        soup.select_one('.portable-infobox [data-source="image"] a[href]'),
        soup.select_one('.portable-infobox [data-source="image"] img[src]'),
        soup.select_one('.portable-infobox [data-source="image"] img[data-src]'),
        soup.select_one('.mw-parser-output img[src]'),
        soup.select_one('.mw-parser-output img[data-src]'),
    ]
    values: list[str] = []
    for node in candidates:
        if not node:
            continue
        for attr in ("href", "src", "data-src"):
            value = node.get(attr)
            if value:
                values.append(str(value))
    usable = next((value for value in values if not value.startswith("data:image")), "")
    return usable


def extract_raw_text(soup: BeautifulSoup) -> str:
    return normalize_whitespace(soup.get_text(" "))


def extract_description_short(soup: BeautifulSoup) -> str:
    paragraph = soup.select_one(".mw-parser-output p")
    return normalize_whitespace(paragraph.get_text(" ") if paragraph else "")


def extract_role_text(soup: BeautifulSoup) -> str:
    node = find_infobox_value(soup, "description")
    if node:
        return normalize_whitespace(node.get_text(" "))
    paragraph = soup.select_one(".mw-parser-output p")
    return normalize_whitespace(paragraph.get_text(" ") if paragraph else "")


def extract_ability_text(soup: BeautifulSoup) -> str:
    node = find_infobox_value(soup, "ability")
    return normalize_whitespace(node.get_text(" ") if node else "")


def extract_character_name(soup: BeautifulSoup) -> str:
    node = find_infobox_value(soup, "character")
    return normalize_whitespace(node.get_text(" ") if node else "")


def collect_section_links_by_headline_id(soup: BeautifulSoup, headline_id: str) -> list[str]:
    headline = soup.select_one(f"span.mw-headline#{headline_id}")
    if not headline:
        return []
    heading = headline.find_parent(["h2", "h3"])
    if not heading:
        return []
    results: list[str] = []
    current = heading.find_next_sibling()
    while current:
        if current.name in {"h2", "h3"}:
            break
        for anchor in current.select("a[title]"):
            title = normalize_whitespace(anchor.get("title") or anchor.get_text(" "))
            if title:
                results.append(title)
        current = current.find_next_sibling()
    return uniq(results)


def extract_restriction_notes(text: str) -> list[str]:
    notes: list[str] = []
    lines = [normalize_whitespace(line) for line in str(text or "").replace("\u00a0", " ").splitlines() if normalize_whitespace(line)]
    for line in lines:
        matches = re.findall(r"[A-Za-z][A-Za-z\s-]*?\((?:PvE|PvP)[^)]+\)", line)
        notes.extend(matches)
    return uniq(notes)


def scrape_fandom_class(title: str) -> dict[str, Any]:
    page = fetch_fandom_page(title)
    soup = BeautifulSoup(page["html"], "html.parser")
    body_text = extract_raw_text(soup)
    record = {
        "name": title,
        "slug_candidate": slugify(title),
        "image_key": f"class_{slugify(title)}_img",
        "image_url": extract_image_url(soup),
        "class_role_text": extract_role_text(soup),
        "class_ability": extract_ability_text(soup),
        "character_name": extract_character_name(soup),
        "weapons": {"primary": [], "secondary": [], "melee": []},
        "notes": [],
        **create_parse_metadata("fandom", [page["url"]]),
    }
    for slot in SLOT_LABELS:
        headline_id = slot["fandom"].replace(" ", "_")
        record["weapons"][slot["key"]] = [name for name in collect_section_links_by_headline_id(soup, headline_id) if name != title]
    if title in {"Assault", "Bulwark"}:
        record["weapons"]["primary"] = []
    record["notes"] = uniq([*record["notes"], *extract_restriction_notes(body_text)])
    return finalize_parse_metadata(record, ["name", "slug_candidate", "image_url", "class_role_text"])


def fetch_fandom_weapon_titles() -> list[str]:
    payload = get_json(
        "https://spacemarine2.fandom.com/api.php",
        params={"action": "query", "list": "categorymembers", "cmtitle": "Category:Weapons", "cmlimit": 200, "format": "json", "origin": "*"},
    )
    members = ((payload.get("query") or {}).get("categorymembers")) or []
    return [entry["title"] for entry in members if entry.get("ns") == 0 and entry.get("title") != "Equipment"]


def scrape_fandom_weapon(title: str) -> dict[str, Any]:
    page = fetch_fandom_page(title)
    soup = BeautifulSoup(page["html"], "html.parser")
    slot_node = find_infobox_value(soup, "slot")
    class_node = find_infobox_value(soup, "class")
    slot_value = normalize_whitespace(slot_node.get_text(" ") if slot_node else "")
    allowed_classes = uniq([normalize_whitespace(anchor.get_text(" ")) for anchor in class_node.select("a")]) if class_node else []
    raw_class_value = ""
    if class_node:
        raw_class_value = str(class_node).replace("<br/>", "\n").replace("<br />", "\n").replace("<br>", "\n")
        raw_class_value = re.sub(r"<[^>]+>", " ", raw_class_value)
    notes = extract_restriction_notes(raw_class_value)
    record = {
        "name": title,
        "slug_candidate": slugify(title),
        "image_key": f"weapon_{slugify(title)}_img",
        "image_url": extract_image_url(soup),
        "slot_type": slot_value.lower(),
        "allowed_classes": allowed_classes,
        "mode_restriction_candidates": derive_mode_restriction_candidates(notes),
        "description_short": extract_description_short(soup),
        "notes": notes,
        **create_parse_metadata("fandom", [page["url"]]),
    }
    return finalize_parse_metadata(record, ["name", "slug_candidate", "slot_type", "allowed_classes"])


def fetch_game8_root() -> tuple[str, BeautifulSoup]:
    url = "https://game8.co/games/Warhammer-40000-Space-Marine-2"
    html = get_html(url)
    return url, BeautifulSoup(html, "html.parser")


def parse_game8_section_links(soup: BeautifulSoup, heading_id: str) -> list[dict[str, str]]:
    heading = soup.select_one(f"#{heading_id}")
    if not heading:
        return []
    links: list[dict[str, str]] = []
    current = heading.find_next_sibling()
    while current:
        if current.name == "h2":
            break
        for anchor in current.select("a.a-link, a.a-btn"):
            title = normalize_whitespace(anchor.get_text(" "))
            href = anchor.get("href")
            img = anchor.select_one("img")
            image_url = (img.get("data-src") or img.get("src") or "") if img else ""
            if title and href and re.search(r"archives/\d+", href):
                links.append({
                    "title": title,
                    "url": requests.compat.urljoin("https://game8.co", href),
                    "image_url": requests.compat.urljoin("https://game8.co", image_url) if image_url else "",
                })
        current = current.find_next_sibling()
    return uniq(links)


def scrape_game8_weapon_detail(entry: dict[str, str]) -> dict[str, Any]:
    html = get_html(entry["url"])
    soup = BeautifulSoup(html, "html.parser")
    classes: list[str] = []
    headings = soup.find_all("h2")
    heading = next((node for node in headings if re.search(r"Classes$", normalize_whitespace(node.get_text(" ")), re.I)), None)
    if heading:
        current = heading.find_next_sibling()
        while current:
            if current.name == "h2":
                break
            for anchor in current.select("a.a-link"):
                class_name = normalize_whitespace(anchor.get_text(" "))
                if class_name in CLASS_TITLES:
                    classes.append(class_name)
            current = current.find_next_sibling()
    return {"name": entry["title"], "url": entry["url"], "allowed_classes": uniq(classes)}


def collect_game8_validation() -> dict[str, Any]:
    _, soup = fetch_game8_root()
    class_entries = [entry for entry in parse_game8_section_links(soup, "hl_5") if entry["title"] in CLASS_TITLES]
    weapon_entries = [entry for entry in parse_game8_section_links(soup, "hl_4") if entry["title"] != "List of All Weapons"]
    weapons = {entry["title"]: scrape_game8_weapon_detail(entry) for entry in weapon_entries}
    return {"classes": {entry["title"]: entry for entry in class_entries}, "weapons": weapons}


def compare_sets(left: list[str], right: list[str]) -> bool:
    return set(left) == set(right)


def attach_validation_notes(classes: list[dict[str, Any]], weapons: list[dict[str, Any]], game8_validation: dict[str, Any]) -> None:
    for class_record in classes:
        game8_class = game8_validation["classes"].get(class_record["name"])
        if not game8_class:
            class_record["notes"].append(conflict_note("Game8 missing / outdated: class absent from Game8 class list."))
        class_record["notes"] = uniq(class_record["notes"])
    for weapon_record in weapons:
        game8_weapon = game8_validation["weapons"].get(weapon_record["name"])
        if not game8_weapon:
            weapon_record["notes"].append(conflict_note("Game8 missing / outdated: weapon absent from Game8 weapon list."))
        elif not compare_sets(weapon_record["allowed_classes"], game8_weapon["allowed_classes"]):
            weapon_record["notes"].append(conflict_note(
                f"Fandom!=Game8 classes: Fandom=[{', '.join(weapon_record['allowed_classes'])}] Game8=[{', '.join(game8_weapon['allowed_classes'])}]"
            ))
        weapon_record["mode_restriction_candidates"] = uniq([
            *weapon_record["mode_restriction_candidates"],
            *derive_mode_restriction_candidates(weapon_record["notes"]),
        ])
        weapon_record["notes"] = uniq(weapon_record["notes"])


def rebuild_class_weapons_from_weapons(classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> None:
    class_map = {entry["name"]: entry for entry in classes}
    slot_lookup = {slot["slot_type"]: slot["key"] for slot in SLOT_LABELS}
    for weapon_record in weapons:
        slot_key = slot_lookup.get(weapon_record["slot_type"])
        if not slot_key:
            continue
        for class_name in weapon_record["allowed_classes"]:
            class_record = class_map.get(class_name)
            if not class_record:
                continue
            class_record["weapons"][slot_key].append(weapon_record["name"])
    for entry in classes:
        entry["weapons"]["primary"] = [] if entry["name"] in {"Assault", "Bulwark"} else uniq(entry["weapons"]["primary"])
        entry["weapons"]["secondary"] = uniq(entry["weapons"]["secondary"])
        entry["weapons"]["melee"] = uniq(entry["weapons"]["melee"])


def cross_check_class_weapon_closure(classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> None:
    weapon_map = {weapon["name"]: weapon for weapon in weapons}
    for class_record in classes:
        for slot in SLOT_LABELS:
            for weapon_name in class_record["weapons"][slot["key"]]:
                weapon = weapon_map.get(weapon_name)
                if not weapon:
                    class_record["notes"].append(conflict_note(f"Fandom class map references missing weapon page: {weapon_name}"))
                    continue
                if weapon["slot_type"] != slot["slot_type"]:
                    class_record["notes"].append(conflict_note(f"Slot mismatch for {weapon_name}: class={slot['slot_type']} weapon={weapon['slot_type']}"))
                if class_record["name"] not in weapon["allowed_classes"]:
                    weapon["notes"].append(conflict_note(
                        f"Reverse mapping mismatch: {class_record['name']} lists {weapon_name}, but weapon page omits the class."
                    ))
        class_record["notes"] = uniq(class_record["notes"])
    for weapon_record in weapons:
        weapon_record["notes"] = uniq(weapon_record["notes"])


def load_talent_rows_by_class() -> dict[str, list[dict[str, Any]]]:
    payload = read_json(TALENT_IMPORT_PATH, {"rows": []})
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        class_name = normalize_whitespace(row.get("class_name"))
        if not class_name:
            continue
        result.setdefault(class_name, []).append(row)
    return result


def normalize_chinese_class_name(english_name: str) -> str:
    return CLASS_NAME_MAP.get(english_name, english_name)


def extract_perk_tree_talents(page_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    results: list[dict[str, str]] = []
    for node in soup.select('.perktree-table .sm2-tooltip'):
        english_name = normalize_whitespace(node.get('data-title') or node.get_text(' '))
        img = node.select_one('img')
        icon_url = (img.get('data-src') or img.get('src') or '') if img else ''
        image_key = img.get('data-image-key') or '' if img else ''
        if english_name:
            results.append({"english_name": english_name, "icon_url": icon_url, "image_key": image_key})
    return uniq(results)


def merge_talent_seeds_with_page_data(class_record: dict[str, Any], rows_by_class: dict[str, list[dict[str, Any]]], page_talents: list[dict[str, str]]) -> list[dict[str, Any]]:
    class_name = normalize_chinese_class_name(class_record['name'])
    chinese_rows = rows_by_class.get(class_name, [])
    items: list[dict[str, Any]] = []
    for index, item in enumerate(page_talents[:TALENT_SLOT_COUNT]):
        row = chinese_rows[index] if index < len(chinese_rows) else {}
        items.append({
            "english_name": item.get("english_name", ""),
            "chinese_name": normalize_whitespace(row.get("talent_name_raw", "")),
            "description": normalize_whitespace(row.get("description", "")),
            "icon_url": item.get("icon_url", ""),
            "image_key": item.get("image_key", ""),
            "index": index,
        })
    return items


def build_real_talent_seeds(class_record: dict[str, Any], rows_by_class: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    page = fetch_fandom_page(class_record['name'])
    page_talents = extract_perk_tree_talents(page['html'])
    return merge_talent_seeds_with_page_data(class_record, rows_by_class, page_talents) if page_talents else []


def talent_coords(index: int) -> tuple[int, int]:
    col = (index // TALENT_GRID_ROWS) + 1
    row = (index % TALENT_GRID_ROWS) + 1
    return col, row


def build_talent_payload(classes: list[dict[str, Any]]) -> dict[str, Any]:
    rows_by_class = load_talent_rows_by_class()
    talents_by_class: dict[str, list[dict[str, Any]]] = {}
    for class_record in classes:
        seeds = build_real_talent_seeds(class_record, rows_by_class)
        items: list[dict[str, Any]] = []
        for item in seeds:
            col, row = talent_coords(int(item.get("index", 0)))
            items.append({
                "class_name": normalize_chinese_class_name(class_record["name"]),
                "english_name": item.get("english_name", ""),
                "chinese_name": item.get("chinese_name", ""),
                "description": item.get("description", ""),
                "icon_url": item.get("icon_url", ""),
                "image_key": item.get("image_key", ""),
                "col": col,
                "row": row,
                "grid_label": f"{col}/{row}",
            })
        talents_by_class[class_record["name"]] = items
    return {"classes": talents_by_class, "manual_action_items": []}


def append_talent_payload(raw_payload: dict[str, Any], talent_payload: dict[str, Any]) -> dict[str, Any]:
    return {**raw_payload, "talents": talent_payload.get("classes", []), "meta": {**raw_payload.get("meta", {}), "talent_coverage": {
        "talent_class_count": len(talent_payload.get("classes", {})),
        "talent_icon_count": sum(len(items) for items in talent_payload.get("classes", {}).values()),
        "talent_icon_downloaded_count": sum(len(items) for items in talent_payload.get("classes", {}).values()),
        "talent_manual_action_count": len(talent_payload.get("manual_action_items", [])),
    }, "talent_manual_action_items": talent_payload.get("manual_action_items", [])}}


def normalize_raw_entities(classes: list[dict[str, Any]], weapons: list[dict[str, Any]], official_assets: list[dict[str, Any]]) -> None:
    for entry in classes:
        entry["notes"] = uniq(entry["notes"])
        entry["parse_warnings"] = uniq(entry["parse_warnings"])
        entry["source_pages"] = uniq(entry["source_pages"])
    for entry in weapons:
        entry["allowed_classes"] = uniq(entry["allowed_classes"])
        entry["notes"] = uniq(entry["notes"])
        entry["parse_warnings"] = uniq(entry["parse_warnings"])
        entry["mode_restriction_candidates"] = uniq(entry["mode_restriction_candidates"])
        entry["source_pages"] = uniq(entry["source_pages"])
    for entry in official_assets:
        entry["notes"] = uniq(entry.get("notes", []))
        entry["parse_warnings"] = uniq(entry["parse_warnings"])
        entry["source_pages"] = uniq(entry["source_pages"])


def build_official_asset_entries(official_anchor: dict[str, str], classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for entry in classes:
        items.append({
            "asset_id": f"official-class-{entry['slug_candidate']}",
            "asset_type": "class",
            "title": f"{entry['name']} official candidate",
            "candidate_page": official_anchor["url"],
            "image_hint": f"{entry['name']} update art candidate",
            "remote_url": entry.get("image_url", ""),
            "intended_bind": entry["slug_candidate"],
            "copyright_note": "Games Workshop intellectual property used under license.",
            "notes": ["Bound from class wiki image."],
            **create_parse_metadata("official", [official_anchor["url"]]),
        })
    for entry in weapons:
        items.append({
            "asset_id": f"wiki-weapon-{entry['slug_candidate']}",
            "asset_type": "weapon",
            "title": f"{entry['name']} wiki image",
            "candidate_page": entry["source_pages"][0] if entry.get("source_pages") else "",
            "image_hint": f"{entry['name']} weapon image",
            "remote_url": entry.get("image_url", ""),
            "intended_bind": entry["slug_candidate"],
            "copyright_note": "Sourced from SM2 wiki imagery for reference display.",
            "notes": ["Bound from weapon wiki image."],
            **create_parse_metadata("wiki", entry.get("source_pages", [])),
        })
    return [finalize_parse_metadata(entry, ["asset_id", "asset_type", "candidate_page", "intended_bind"]) for entry in items]


def collect_parse_issues(classes: list[dict[str, Any]], weapons: list[dict[str, Any]], official_assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for entry in classes:
        if entry["parse_warnings"]:
            issues.append(build_review_record("class", entry["name"], " | ".join(entry["parse_warnings"]), entry["source_pages"]))
    for entry in weapons:
        if entry["parse_warnings"]:
            issues.append(build_review_record("weapon", entry["name"], " | ".join(entry["parse_warnings"]), entry["source_pages"]))
    for entry in official_assets:
        if entry["parse_warnings"]:
            issues.append(build_review_record("official_asset", entry["title"], " | ".join(entry["parse_warnings"]), entry["source_pages"]))
    return issues


def collect_drift_samples(classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> dict[str, list[str]]:
    required_class_names = {"Techmarine"}
    required_weapon_names = {"Melta Rifle", "Heavy Firearms Melee"}
    return {
        "classes": [entry["name"] for entry in classes if entry["name"] in required_class_names],
        "weapons": [entry["name"] for entry in weapons if entry["name"] in required_weapon_names],
    }


def build_sample_field_logs(classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    sample_names = {"Techmarine", "Melta Rifle", "Heavy Firearms Melee"}
    return {
        "classes": [{"name": entry["name"], "missing_fields": entry["missing_fields"], "parse_warnings": entry["parse_warnings"]} for entry in classes if entry["name"] in sample_names],
        "weapons": [{"name": entry["name"], "missing_fields": entry["missing_fields"], "parse_warnings": entry["parse_warnings"]} for entry in weapons if entry["name"] in sample_names],
    }


def build_parse_report(classes: list[dict[str, Any]], weapons: list[dict[str, Any]], official_assets: list[dict[str, Any]]) -> dict[str, Any]:
    issues = collect_parse_issues(classes, weapons, official_assets)
    return {
        "critical_field_logs": [
            *[{"entity_type": "class", "name": entry["name"], "missing_fields": entry["missing_fields"]} for entry in classes if entry["missing_fields"]],
            *[{"entity_type": "weapon", "name": entry["name"], "missing_fields": entry["missing_fields"]} for entry in weapons if entry["missing_fields"]],
            *[{"entity_type": "official_asset", "name": entry["title"], "missing_fields": entry["missing_fields"]} for entry in official_assets if entry["missing_fields"]],
        ],
        "issue_count": len(issues),
    }


def build_drift_report(classes: list[dict[str, Any]], weapons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tracked_samples": collect_drift_samples(classes, weapons), "field_logs": build_sample_field_logs(classes, weapons)}


def to_raw_class_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "slug_candidate": entry["slug_candidate"],
        "image_key": entry["image_key"],
        "image_url": entry["image_url"],
        "class_role_text": entry["class_role_text"],
        "class_ability": entry["class_ability"],
        "character_name": entry["character_name"],
        "weapons": entry["weapons"],
        "notes": entry["notes"],
        "source_pages": entry["source_pages"],
        "source_type": entry["source_type"],
        "parse_warnings": entry["parse_warnings"],
        "parse_degraded": entry["parse_degraded"],
        "missing_fields": entry["missing_fields"],
    }


def to_raw_weapon_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "slug_candidate": entry["slug_candidate"],
        "image_key": entry["image_key"],
        "image_url": entry["image_url"],
        "slot_type": entry["slot_type"],
        "allowed_classes": entry["allowed_classes"],
        "mode_restriction_candidates": entry["mode_restriction_candidates"],
        "description_short": entry["description_short"],
        "notes": entry["notes"],
        "source_pages": entry["source_pages"],
        "source_type": entry["source_type"],
        "parse_warnings": entry["parse_warnings"],
        "parse_degraded": entry["parse_degraded"],
        "missing_fields": entry["missing_fields"],
    }


def to_raw_official_asset_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": entry["asset_id"],
        "asset_type": entry["asset_type"],
        "title": entry["title"],
        "candidate_page": entry["candidate_page"],
        "image_hint": entry["image_hint"],
        "remote_url": entry["remote_url"],
        "intended_bind": entry["intended_bind"],
        "copyright_note": entry["copyright_note"],
        "notes": entry["notes"],
        "source_pages": entry["source_pages"],
        "source_type": entry["source_type"],
        "parse_warnings": entry["parse_warnings"],
        "parse_degraded": entry["parse_degraded"],
        "missing_fields": entry["missing_fields"],
    }


def build_review_seed(raw_classes: list[dict[str, Any]], raw_weapons: list[dict[str, Any]], raw_official_assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": collect_parse_issues(raw_classes, raw_weapons, raw_official_assets)}


def build_raw_payload(classes: list[dict[str, Any]], weapons: list[dict[str, Any]], official_assets: list[dict[str, Any]], official_anchor: dict[str, str], game8_validation: dict[str, Any]) -> dict[str, Any]:
    raw_classes = [to_raw_class_payload(entry) for entry in classes]
    raw_weapons = [to_raw_weapon_payload(entry) for entry in weapons]
    raw_official_assets = [to_raw_official_asset_payload(entry) for entry in official_assets]
    conflict_count = sum(len([note for note in entry["notes"] if str(note).startswith(CONFLICT_PREFIX)]) for entry in raw_classes)
    conflict_count += sum(len([note for note in entry["notes"] if str(note).startswith(CONFLICT_PREFIX)]) for entry in raw_weapons)
    return {
        "meta": {
            "generated_at": requests.utils.datetime_to_header(requests.utils.parse_header_links('<dummy>; rel="self"')[0].get('dummy', '')) if False else None,
            "version_anchor": VERSION_ANCHOR,
            "official_anchor": official_anchor,
            "sources": {
                "official": official_anchor["url"],
                "fandom": "https://spacemarine2.fandom.com",
                "game8": "https://game8.co/games/Warhammer-40000-Space-Marine-2",
            },
            "game8_known_class_count": len(game8_validation["classes"]),
            "game8_known_weapon_count": len(game8_validation["weapons"]),
            "conflict_count": conflict_count,
            "parse_report": build_parse_report(raw_classes, raw_weapons, raw_official_assets),
            "drift_report": build_drift_report(raw_classes, raw_weapons),
        },
        "classes": raw_classes,
        "weapons": raw_weapons,
        "official_assets": raw_official_assets,
        "review_seed": build_review_seed(raw_classes, raw_weapons, raw_official_assets),
    }


def build_markdown_table(raw_payload: dict[str, Any]) -> str:
    lines = [
        "# Space Marine 2 Data Review",
        "",
        f"- Generated At: {raw_payload['meta']['generated_at']}",
        f"- Version Anchor: {raw_payload['meta']['version_anchor']}",
        f"- Conflict Count: {raw_payload['meta']['conflict_count']}",
        f"- Official Anchor: {raw_payload['meta']['official_anchor']['url']}",
        "",
        "## 职业表",
        "",
        "| Class | Image URL | Primary | Secondary | Melee | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in raw_payload["classes"]:
        lines.append(f"| {entry['name']} | {entry['image_url']} | {', '.join(entry['weapons']['primary']) or '[]'} | {', '.join(entry['weapons']['secondary']) or '[]'} | {', '.join(entry['weapons']['melee']) or '[]'} | {'<br>'.join(entry['notes'])} |")
    lines.extend(["", "## 武器表", "", "| Weapon | Slot | Image URL | Allowed Classes | Notes |", "| --- | --- | --- | --- | --- |"])
    for entry in raw_payload["weapons"]:
        lines.append(f"| {entry['name']} | {entry['slot_type']} | {entry['image_url']} | {', '.join(entry['allowed_classes']) or '[]'} | {'<br>'.join(entry['notes'])} |")
    lines.append("")
    return "\n".join(lines)


def write_outputs(raw_payload: dict[str, Any], markdown: str) -> None:
    ensure_directories()
    write_json(RAW_OUTPUT_PATH, raw_payload)
    TABLE_OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    weapon_map = {
        "classes": [
            {
                "class_name": entry["name"],
                "slug_candidate": entry["slug_candidate"],
                "weapons": entry["weapons"],
            }
            for entry in raw_payload["classes"]
        ]
    }
    write_json(CLASS_WEAPON_MAP_OUTPUT_PATH, weapon_map)


def main() -> int:
    official_anchor = fetch_official_patch_anchor()
    classes = [scrape_fandom_class(title) for title in CLASS_TITLES]
    weapon_titles = fetch_fandom_weapon_titles()
    weapons = [scrape_fandom_weapon(title) for title in weapon_titles]
    if not any(entry["name"] == "Techmarine" for entry in classes):
        raise RuntimeError("Techmarine class missing from Fandom output.")
    if not any(entry["name"] == "Omnissiah Axe" for entry in weapons):
        raise RuntimeError("Omnissiah Axe missing from Fandom output.")
    game8_validation = collect_game8_validation()
    rebuild_class_weapons_from_weapons(classes, weapons)
    attach_validation_notes(classes, weapons, game8_validation)
    cross_check_class_weapon_closure(classes, weapons)
    official_assets = build_official_asset_entries(official_anchor, classes, weapons)
    normalize_raw_entities(classes, weapons, official_assets)
    talent_payload = build_talent_payload(classes)
    raw_payload = append_talent_payload(build_raw_payload(classes, weapons, official_assets, official_anchor, game8_validation), talent_payload)
    raw_payload["meta"]["generated_at"] = __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
    markdown = build_markdown_table(raw_payload)
    write_outputs(raw_payload, markdown)
    print(f"[sm2-randomizer] Wrote raw wiki payload to: {RAW_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
