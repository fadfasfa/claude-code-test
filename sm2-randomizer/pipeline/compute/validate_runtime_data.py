from __future__ import annotations

"""运行期数据契约校验入口。

校验 classes/talents/meta 三个运行文件的结构、语义与资源路径一致性，并输出校验报告。
"""

import argparse
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import APP_ASSETS_DIR, APP_DATA_DIR, MANUAL_SOURCE_FILE, VALIDATION_REPORT_FILE, ensure_directories, read_json, write_json

EXPECTED_CLASS_COUNT = 7
EXPECTED_PRIMARY_COUNT = 17
EXPECTED_SECONDARY_COUNT = 5
EXPECTED_MELEE_COUNT = 8
EXPECTED_WEAPON_COUNT = 30
EXPECTED_TALENT_CLASS_COUNT = 7
EXPECTED_TALENT_NODE_COUNT = 24
EXPECTED_TALENT_GRID = {
    "cols": 8,
    "rows": 3,
    "label_format": "col/row",
    "order": "column-major",
}
FORBIDDEN_RUNTIME_FIELDS = {"description_raw", "source_meta", "field_source_policy", "strategy_terms", "strategy_modifier_pools"}
CLASS_SLUG_TO_ZH_NAME = {
    "tactical": "战术兵",
    "assault": "突击兵",
    "vanguard": "先锋兵",
    "bulwark": "重装兵",
    "sniper": "狙击兵",
    "heavy": "特战兵",
    "techmarine": "技术军士",
}
REQUIRED_RUNTIME_FILES = ("classes.json", "talents.json", "meta.json")
LEGACY_RUNTIME_FILES = ("loadouts.json", "talent_details.json")
MAX_NEGATIVE_LABEL_LENGTH = 14
FORBIDDEN_CLASS_FIELDS = {"skills"}
FORBIDDEN_LOADOUT_POOL_FIELDS = {"all"}


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _is_local_asset_path(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("http://") and not text.startswith("https://")


def _walk(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{path}.{key}" if path != "$" else key
            yield from _walk(child, next_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    else:
        yield path, value


def _is_ascii_only_text(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.isascii()


def _contains_forbidden_fields(payload: Any) -> list[str]:
    hits: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_RUNTIME_FIELDS:
                hits.append(key)
            hits.extend(_contains_forbidden_fields(value))
    elif isinstance(payload, list):
        for item in payload:
            hits.extend(_contains_forbidden_fields(item))
    return hits


def _validate_scrape_perks_contract() -> list[dict[str, str]]:
    source_path = PROJECT_ROOT / "pipeline" / "collect" / "wiki" / "scrape_perks.py"
    if not source_path.exists():
        return [_issue("error", "SCRAPE_PERKS_SOURCE", "缺少 pipeline/collect/wiki/scrape_perks.py")]

    source_text = source_path.read_text(encoding="utf-8")
    match = re.search(r"def build_existing_talent_maps\(\).*?(?=\ndef |\Z)", source_text, flags=re.S)
    if not match:
        return [_issue("error", "SCRAPE_PERKS_SOURCE", "无法定位 build_existing_talent_maps 定义")]
    body = match.group(0)
    if "class_skill_entries" in body or "MANUAL_SOURCE_FILE" in body:
        return [_issue("error", "SCRAPE_PERKS_CONTRACT", "build_existing_talent_maps() 不得再从 class_skill_entries 构造天赋 fallback")]
    return []


def _load_manual_skill_descriptions() -> set[str]:
    manual = read_json(MANUAL_SOURCE_FILE, {"class_skill_entries": {}})
    entries = manual.get("class_skill_entries", {}) if isinstance(manual, dict) else {}
    if not isinstance(entries, dict):
        return set()
    values: set[str] = set()
    for class_items in entries.values():
        if not isinstance(class_items, list):
            continue
        for item in class_items:
            if not isinstance(item, dict):
                continue
            detail = str(item.get("description", "")).strip()
            if detail and detail != "/":
                values.add(detail)
    return values


def validate_runtime_data(
    target_dir: Path | None = None,
    report_path: Path | None = None,
    assets_dir: Path | None = None,
) -> dict:
    ensure_directories()
    runtime_dir = target_dir or APP_DATA_DIR
    resolved_assets_dir = assets_dir or APP_ASSETS_DIR
    payloads = {name: read_json(runtime_dir / name, {}) for name in REQUIRED_RUNTIME_FILES}
    structure_issues: list[dict[str, str]] = []
    semantic_issues: list[dict[str, str]] = []

    for name, payload in payloads.items():
        if not payload:
            structure_issues.append(_issue("error", "MISSING_FILE", f"{name} 未生成或为空"))

    for legacy_name in LEGACY_RUNTIME_FILES:
        if (runtime_dir / legacy_name).exists():
            structure_issues.append(_issue("error", "LEGACY_RUNTIME_FILE", f"{legacy_name} 不应继续出现在 app/data 中"))

    classes = payloads["classes.json"].get("classes", []) if isinstance(payloads["classes.json"], dict) else []
    talent_classes = payloads["talents.json"].get("classes", []) if isinstance(payloads["talents.json"], dict) else []
    meta = payloads["meta.json"] if isinstance(payloads["meta.json"], dict) else {}
    build = meta.get("build", {}) if isinstance(meta.get("build"), dict) else {}

    forbidden_hits = []
    for name, payload in payloads.items():
        for hit in _contains_forbidden_fields(payload):
            forbidden_hits.append(f"{name}:{hit}")
    for hit in forbidden_hits:
        semantic_issues.append(_issue("error", "FORBIDDEN_RUNTIME_FIELD", f"运行期数据不应包含中间字段：{hit}"))

    for name, payload in payloads.items():
        for path, value in _walk(payload, name):
            if value == "/":
                semantic_issues.append(_issue("error", "PLACEHOLDER_VALUE", f"{path} 仍保留 '/' 占位符"))

    class_slugs: set[str] = set()
    slot_sets = {"primary": set(), "secondary": set(), "melee": set()}
    for entry in classes:
        if not isinstance(entry, dict):
            structure_issues.append(_issue("error", "CLASS_ENTRY", "classes.json 存在非对象条目"))
            continue
        slug = str(entry.get("slug", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not slug or not name:
            structure_issues.append(_issue("error", "CLASS_ENTRY", f"职业缺少 slug/name：{entry}"))
            continue
        if slug in class_slugs:
            structure_issues.append(_issue("error", "DUPLICATE_CLASS", f"职业 slug 重复：{slug}"))
        class_slugs.add(slug)

        image_path = entry.get("image_path")
        if not _is_local_asset_path(image_path):
            structure_issues.append(_issue("error", "CLASS_IMAGE_PATH", f"{slug} 缺少合法本地职业图：{image_path}"))
        elif not (resolved_assets_dir / str(image_path)).exists():
            structure_issues.append(_issue("error", "CLASS_IMAGE_FILE", f"{slug} 职业图文件不存在：{image_path}"))

        for forbidden in FORBIDDEN_CLASS_FIELDS:
            if forbidden in entry:
                structure_issues.append(_issue("error", "FORBIDDEN_CLASS_FIELD", f"{slug} 不应包含字段：{forbidden}"))

        pools = entry.get("loadout_pools", {})
        if not isinstance(pools, dict):
            structure_issues.append(_issue("error", "LOADOUT_POOLS", f"{slug} 缺少合法 loadout_pools"))
            continue
        seen_class_weapons: set[str] = set()
        for slot in ("primary", "secondary", "melee"):
            items = pools.get(slot, [])
            if not isinstance(items, list):
                structure_issues.append(_issue("error", "LOADOUT_POOL_SLOT", f"{slug}.{slot} 必须为数组"))
                continue
            for item in items:
                if not isinstance(item, dict):
                    structure_issues.append(_issue("error", "LOADOUT_POOL_ENTRY", f"{slug}.{slot} 存在非对象条目"))
                    continue
                weapon_slug = str(item.get("slug", "")).strip()
                if not weapon_slug:
                    structure_issues.append(_issue("error", "LOADOUT_POOL_ENTRY", f"{slug}.{slot} 缺少 slug：{item}"))
                    continue
                slot_sets[slot].add(weapon_slug)
                seen_class_weapons.add(weapon_slug)
                image_path = item.get("image_path")
                if not _is_local_asset_path(image_path):
                    structure_issues.append(_issue("error", "WEAPON_IMAGE_PATH", f"{slug}.{slot}.{weapon_slug} 缺少合法本地图标"))
                    continue
                if not (resolved_assets_dir / str(image_path)).exists():
                    structure_issues.append(_issue("error", "WEAPON_IMAGE_FILE", f"{weapon_slug} 图标文件不存在：{image_path}"))
                if "original_name" in item:
                    structure_issues.append(_issue("error", "FORBIDDEN_WEAPON_FIELD", f"{slug}.{slot}.{weapon_slug} 不应包含字段：original_name"))
        for forbidden in FORBIDDEN_LOADOUT_POOL_FIELDS:
            if forbidden in pools:
                structure_issues.append(_issue("error", "FORBIDDEN_LOADOUT_POOL_FIELD", f"{slug}.loadout_pools 不应包含字段：{forbidden}"))

    primary_count = len(slot_sets["primary"])
    secondary_count = len(slot_sets["secondary"])
    melee_count = len(slot_sets["melee"])
    weapon_count = primary_count + secondary_count + melee_count

    if len(class_slugs) != EXPECTED_CLASS_COUNT:
        structure_issues.append(_issue("error", "CLASS_COUNT", f"职业数量应为 {EXPECTED_CLASS_COUNT}，当前为 {len(class_slugs)}"))
    if primary_count != EXPECTED_PRIMARY_COUNT:
        structure_issues.append(_issue("error", "PRIMARY_COUNT", f"主武器数量应为 {EXPECTED_PRIMARY_COUNT}，当前为 {primary_count}"))
    if secondary_count != EXPECTED_SECONDARY_COUNT:
        structure_issues.append(_issue("error", "SECONDARY_COUNT", f"副武器数量应为 {EXPECTED_SECONDARY_COUNT}，当前为 {secondary_count}"))
    if melee_count != EXPECTED_MELEE_COUNT:
        structure_issues.append(_issue("error", "MELEE_COUNT", f"近战武器数量应为 {EXPECTED_MELEE_COUNT}，当前为 {melee_count}"))
    if weapon_count != EXPECTED_WEAPON_COUNT:
        structure_issues.append(_issue("error", "WEAPON_COUNT", f"正式武器数量应为 {EXPECTED_WEAPON_COUNT}，当前为 {weapon_count}"))

    if build.get("source_mode") != "hybrid_now_wiki_ready":
        semantic_issues.append(_issue("warning", "SOURCE_MODE", "build.source_mode 未固定为 hybrid_now_wiki_ready"))

    talent_slugs: set[str] = set()
    skill_description_set = _load_manual_skill_descriptions()
    missing_description_by_class: dict[str, int] = {}
    missing_description_count = 0
    for entry in talent_classes:
        if not isinstance(entry, dict):
            structure_issues.append(_issue("error", "TALENT_CLASS_ENTRY", "talents.json 存在非对象条目"))
            continue
        class_slug = str(entry.get("class_slug", "")).strip()
        class_name = str(entry.get("class_name", "")).strip()
        if not class_slug or not class_name:
            structure_issues.append(_issue("error", "TALENT_CLASS_ENTRY", f"天赋职业缺少 class_slug/class_name：{entry}"))
            continue
        talent_slugs.add(class_slug)
        grid_spec = entry.get("grid_spec", {})
        if grid_spec != EXPECTED_TALENT_GRID:
            structure_issues.append(_issue("error", "TALENT_GRID_SPEC", f"{class_slug} 的 grid_spec 不符合预期"))
        nodes = entry.get("nodes", [])
        if not isinstance(nodes, list):
            structure_issues.append(_issue("error", "TALENT_NODES", f"{class_slug} 的 nodes 必须为数组"))
            continue
        if len(nodes) != EXPECTED_TALENT_NODE_COUNT:
            structure_issues.append(_issue("error", "TALENT_NODE_COUNT", f"{class_slug} 天赋节点数量应为 {EXPECTED_TALENT_NODE_COUNT}，当前为 {len(nodes)}"))
        seen_coords: set[tuple[int, int]] = set()
        for node in nodes:
            if not isinstance(node, dict):
                structure_issues.append(_issue("error", "TALENT_NODE_ENTRY", f"{class_slug} 存在非对象天赋节点"))
                continue
            talent_slug = str(node.get("talent_slug", "")).strip()
            if not talent_slug:
                structure_issues.append(_issue("error", "TALENT_NODE_ENTRY", f"{class_slug} 存在缺少 talent_slug 的节点"))
                continue
            try:
                col = int(node.get("col"))
                row = int(node.get("row"))
            except (TypeError, ValueError):
                structure_issues.append(_issue("error", "TALENT_NODE_COORD", f"{class_slug}/{talent_slug} 缺少合法坐标"))
                continue
            coord = (col, row)
            if coord in seen_coords:
                structure_issues.append(_issue("error", "TALENT_NODE_COORD_DUP", f"{class_slug} 出现重复矩阵坐标：{col}/{row}"))
            seen_coords.add(coord)
            grid_label = str(node.get("grid_label", "")).strip()
            if grid_label != f"{col}/{row}":
                structure_issues.append(_issue("error", "TALENT_GRID_LABEL", f"{class_slug}/{talent_slug} 的 grid_label 与坐标不一致"))
            icon_path = node.get("icon_path")
            if not _is_local_asset_path(icon_path):
                structure_issues.append(_issue("error", "TALENT_ICON_PATH", f"{class_slug}/{talent_slug} 缺少合法本地图标"))
            else:
                normalized_icon_path = str(icon_path)
                expected_class_name = CLASS_SLUG_TO_ZH_NAME.get(class_slug, class_name)
                expected_prefix = f"talents/{expected_class_name}/"
                if not normalized_icon_path.startswith(expected_prefix):
                    structure_issues.append(_issue("error", "TALENT_ICON_NAMING", f"{class_slug}/{talent_slug} 图标路径未落到中文目录：{icon_path}"))
                if not (resolved_assets_dir / normalized_icon_path).exists():
                    structure_issues.append(_issue("error", "TALENT_ICON_FILE", f"{class_slug}/{talent_slug} 图标文件不存在：{icon_path}"))
            zh_name = str(node.get("talent_name_zh", "") or "").strip()
            en_name = str(node.get("talent_name_en", "") or "").strip()
            if zh_name and zh_name == en_name and zh_name.isascii():
                semantic_issues.append(_issue("error", "PSEUDO_ZH_TALENT_NAME", f"{class_slug}/{talent_slug} 仍保留伪中文天赋名：{zh_name}"))
            description = str(node.get("description", "") or "").strip()
            if not description:
                missing_description_count += 1
                missing_description_by_class[class_slug] = missing_description_by_class.get(class_slug, 0) + 1
            elif _is_ascii_only_text(description):
                semantic_issues.append(_issue("error", "TALENT_DESCRIPTION_NOT_ZH", f"{class_slug}/{talent_slug} 的 description 仍是 ASCII-only 文本"))
            elif description in skill_description_set:
                semantic_issues.append(_issue("error", "TALENT_DESC_POLLUTED", f"{class_slug}/{talent_slug} 使用了职业技能描述污染文本"))

    if len(talent_slugs) != EXPECTED_TALENT_CLASS_COUNT:
        structure_issues.append(_issue("error", "TALENT_CLASS_COUNT", f"天赋职业数量应为 {EXPECTED_TALENT_CLASS_COUNT}，当前为 {len(talent_slugs)}"))

    semantic_issues.extend(_validate_scrape_perks_contract())

    positive_pool = meta.get("positive_modifier_pool", [])
    negative_pool = meta.get("negative_modifier_pool", [])
    negative_rules = meta.get("negative_modifier_rules", {})
    if not isinstance(positive_pool, list) or not positive_pool:
        structure_issues.append(_issue("error", "POSITIVE_MODIFIER_POOL", "positive_modifier_pool 缺失或为空"))
    else:
        for item in positive_pool:
            if not isinstance(item, dict):
                structure_issues.append(_issue("error", "POSITIVE_MODIFIER_ENTRY", "positive_modifier_pool 存在非对象条目"))
                continue
            if not str(item.get("key", "")).strip() or not str(item.get("name", "")).strip() or not str(item.get("detail", "")).strip():
                structure_issues.append(_issue("error", "POSITIVE_MODIFIER_ENTRY", f"正面词条缺少 key/name/detail：{item}"))

    if not isinstance(negative_pool, list) or not negative_pool:
        structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_POOL", "negative_modifier_pool 缺失或为空"))
    if not isinstance(negative_rules, dict):
        structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_RULES", "negative_modifier_rules 缺失或格式错误"))
        negative_rules = {}
    exact_conflicts = negative_rules.get("exact_conflicts", [])
    quota_limits = negative_rules.get("quota_limits", {})
    title_aliases = negative_rules.get("title_aliases", {})
    if not isinstance(exact_conflicts, list) or not exact_conflicts:
        structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_CONFLICTS", "exact_conflicts 缺失或为空"))
    if not isinstance(quota_limits, dict) or not quota_limits:
        structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_QUOTAS", "quota_limits 缺失或为空"))
    if not isinstance(title_aliases, dict) or not title_aliases:
        structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_ALIASES", "title_aliases 缺失或为空"))

    pool_keys = {
        str(item.get("key", "")).strip()
        for item in negative_pool
        if isinstance(item, dict) and str(item.get("key", "")).strip()
    }
    for item in negative_pool:
        if not isinstance(item, dict):
            structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_ENTRY", "negative_modifier_pool 存在非对象条目"))
            continue
        key = str(item.get("key", "")).strip()
        name = str(item.get("name", "")).strip()
        detail = str(item.get("detail", "")).strip()
        label = str(item.get("label", "")).strip()
        if not key or not name or not detail or not label:
            structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_ENTRY", f"负面词条缺少 key/name/detail/label：{item}"))
        if len(label) > MAX_NEGATIVE_LABEL_LENGTH:
            structure_issues.append(_issue("error", "NEGATIVE_LABEL_TOO_LONG", f"{key} 的 label 超过上限 {MAX_NEGATIVE_LABEL_LENGTH}：{label}"))
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list):
            structure_issues.append(_issue("error", "NEGATIVE_ALIASES_TYPE", f"{key} 的 aliases 必须为数组"))
        else:
            cleaned = [str(alias).strip() for alias in aliases if str(alias).strip()]
            if len(cleaned) != len(set(cleaned)):
                structure_issues.append(_issue("error", "NEGATIVE_ALIASES_DUPLICATED", f"{key} 的 aliases 存在重复"))
            derived_aliases = {name, label, detail, label.split("：")[0].strip()}
            redundant = [alias for alias in cleaned if alias in derived_aliases]
            if redundant:
                structure_issues.append(_issue("error", "NEGATIVE_ALIASES_REDUNDANT", f"{key} 的 aliases 含派生冗余值：{', '.join(redundant)}"))
        for tag in item.get("core_tags", []):
            if str(tag).strip() and str(tag).strip() not in quota_limits:
                structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_QUOTA_TAG", f"{key} 使用了未声明的 quota tag：{tag}"))

    for rule in exact_conflicts if isinstance(exact_conflicts, list) else []:
        if not isinstance(rule, dict):
            structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_CONFLICT_RULE", "exact_conflicts 存在非对象条目"))
            continue
        keys = [str(key).strip() for key in rule.get("keys", []) if str(key).strip()]
        if len(keys) < 2:
            structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_CONFLICT_RULE", f"冲突规则至少需要 2 个 key：{rule}"))
            continue
        for key in keys:
            if key not in pool_keys:
                structure_issues.append(_issue("error", "NEGATIVE_MODIFIER_CONFLICT_KEY", f"冲突规则引用了未知 key：{key}"))

    issues = [*structure_issues, *semantic_issues]
    report = {
        "summary": {
            "class_count": len(class_slugs),
            "weapon_count": weapon_count,
            "primary_count": primary_count,
            "secondary_count": secondary_count,
            "melee_count": melee_count,
            "talent_class_count": len(talent_slugs),
            "talent_missing_description_count": missing_description_count,
            "structure_issue_count": len(structure_issues),
            "semantic_issue_count": len(semantic_issues),
            "issue_count": len(issues),
        },
        "talent_missing_description_by_class": missing_description_by_class,
        "structure_issues": structure_issues,
        "semantic_issues": semantic_issues,
        "issues": issues,
    }
    write_json(report_path or VALIDATION_REPORT_FILE, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate runtime contract files against the expected app/data schema.")
    parser.add_argument("--target-dir", default=str(APP_DATA_DIR), help="Directory containing classes.json, talents.json, and meta.json.")
    parser.add_argument("--report-path", default=str(VALIDATION_REPORT_FILE), help="Where to write the validation report JSON.")
    parser.add_argument("--assets-dir", default=str(APP_ASSETS_DIR), help="Directory containing runtime asset files referenced by image_path/icon_path.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    validate_runtime_data(
        target_dir=Path(args.target_dir).resolve(),
        report_path=Path(args.report_path).resolve(),
        assets_dir=Path(args.assets_dir).resolve(),
    )
