from __future__ import annotations

"""候选运行数据发布控制入口。

负责候选与当前运行数据的差异生成、应用/清理决策及候选目录生命周期管理。
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import APP_DATA_DIR, EXCEL_IMPORT_REPORT_FILE, PIPELINE_TMP_PUBLISH_DIR, VALIDATION_REPORT_FILE, read_json, write_json
from pipeline.compute.validate_runtime_data import validate_runtime_data

RUNTIME_FILES = ("classes.json", "talents.json", "meta.json")
DIFF_JSON_NAME = "diff_summary.json"
DIFF_MD_NAME = "diff_summary.md"


def _load_payloads(directory: Path) -> dict[str, Any]:
    return {name: read_json(directory / name, {}) for name in RUNTIME_FILES}


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


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(child) for child in value]
    return value


def _diff_file(candidate: Any, current: Any) -> dict[str, Any]:
    candidate_map = {path: value for path, value in _walk(_normalize(candidate))}
    current_map = {path: value for path, value in _walk(_normalize(current))}
    candidate_paths = set(candidate_map)
    current_paths = set(current_map)

    added = sorted(candidate_paths - current_paths)
    removed = sorted(current_paths - candidate_paths)
    changed = sorted(path for path in candidate_paths & current_paths if candidate_map[path] != current_map[path])

    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added_paths": added[:200],
        "removed_paths": removed[:200],
        "changed_paths": changed[:200],
    }


def _collect_loadout_slugs(classes_payload: dict[str, Any]) -> set[str]:
    slugs: set[str] = set()
    for entry in classes_payload.get("classes", []) if isinstance(classes_payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        pools = entry.get("loadout_pools", {})
        if not isinstance(pools, dict):
            continue
        for slot_items in pools.values():
            if not isinstance(slot_items, list):
                continue
            for item in slot_items:
                if isinstance(item, dict):
                    slug = str(item.get("slug", "")).strip()
                    if slug:
                        slugs.add(slug)
    return slugs


def _collect_class_slugs(classes_payload: dict[str, Any]) -> set[str]:
    return {
        str(entry.get("slug", "")).strip()
        for entry in (classes_payload.get("classes", []) if isinstance(classes_payload, dict) else [])
        if isinstance(entry, dict) and str(entry.get("slug", "")).strip()
    }


def _collect_talent_descriptions(talents_payload: dict[str, Any]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for group in talents_payload.get("classes", []) if isinstance(talents_payload, dict) else []:
        if not isinstance(group, dict):
            continue
        class_slug = str(group.get("class_slug", "")).strip()
        for node in group.get("nodes", []) if isinstance(group.get("nodes"), list) else []:
            if not isinstance(node, dict):
                continue
            talent_slug = str(node.get("talent_slug", "")).strip()
            if class_slug and talent_slug:
                result[(class_slug, talent_slug)] = str(node.get("description", "") or "")
    return result


def _modifier_identity(item: dict[str, Any]) -> str:
    for field in ("key", "name", "title", "label"):
        value = str(item.get(field, "") or "").strip()
        if value:
            return value
    return ""


def _collect_modifier_items(meta_payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    items = meta_payload.get(field_name, []) if isinstance(meta_payload, dict) else []
    result: dict[str, Any] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        key = _modifier_identity(item)
        if key:
            result[key] = _normalize(item)
    return result


def _build_modifier_changes(cand_meta: dict[str, Any], cur_meta: dict[str, Any]) -> dict[str, Any]:
    positive = _diff_modifier_pool(cand_meta, cur_meta, "positive_modifier_pool")
    negative = _diff_modifier_pool(cand_meta, cur_meta, "negative_modifier_pool")
    rules = _diff_file(cand_meta.get("negative_modifier_rules", {}), cur_meta.get("negative_modifier_rules", {}))
    return {
        "positive_modifier_pool": positive,
        "negative_modifier_pool": negative,
        "negative_modifier_rules": rules,
        "has_changes": bool(
            positive["added_count"]
            or positive["removed_count"]
            or positive["changed_count"]
            or rules["added_count"]
            or rules["removed_count"]
            or rules["changed_count"]
            or negative["added_count"]
            or negative["removed_count"]
            or negative["changed_count"]
        ),
    }


def _diff_modifier_pool(cand_meta: dict[str, Any], cur_meta: dict[str, Any], field_name: str) -> dict[str, Any]:
    candidate = _collect_modifier_items(cand_meta, field_name)
    current = _collect_modifier_items(cur_meta, field_name)
    candidate_keys = set(candidate)
    current_keys = set(current)
    changed = sorted(key for key in candidate_keys & current_keys if candidate[key] != current[key])
    added = sorted(candidate_keys - current_keys)
    removed = sorted(current_keys - candidate_keys)
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added_keys": added[:100],
        "removed_keys": removed[:100],
        "changed_keys": changed[:100],
    }


def _build_semantic_changes(
    candidate_payloads: dict[str, Any],
    current_payloads: dict[str, Any],
) -> dict[str, Any]:
    """构建分发者可读的语义变更清单：武器/职业增删、天赋描述变更、版本变更、
    新增待审项、退化与对齐状态。供 diff_summary.md 与 candidate-status 展示。"""
    cand_classes = candidate_payloads.get("classes.json", {}) if isinstance(candidate_payloads.get("classes.json"), dict) else {}
    cur_classes = current_payloads.get("classes.json", {}) if isinstance(current_payloads.get("classes.json"), dict) else {}
    cand_weapon_slugs = _collect_loadout_slugs(cand_classes)
    cur_weapon_slugs = _collect_loadout_slugs(cur_classes)
    cand_class_slugs = _collect_class_slugs(cand_classes)
    cur_class_slugs = _collect_class_slugs(cur_classes)

    cand_talents = candidate_payloads.get("talents.json", {}) if isinstance(candidate_payloads.get("talents.json"), dict) else {}
    cur_talents = current_payloads.get("talents.json", {}) if isinstance(current_payloads.get("talents.json"), dict) else {}
    cand_desc = _collect_talent_descriptions(cand_talents)
    cur_desc = _collect_talent_descriptions(cur_talents)
    changed: list[str] = []
    for key in cand_desc.keys() & cur_desc.keys():
        if cand_desc[key] != cur_desc[key]:
            class_slug, talent_slug = key
            changed.append(f"{class_slug}/{talent_slug}")
    changed_descriptions = sorted(changed)

    cand_meta = candidate_payloads.get("meta.json", {}) if isinstance(candidate_payloads.get("meta.json"), dict) else {}
    cur_meta = current_payloads.get("meta.json", {}) if isinstance(current_payloads.get("meta.json"), dict) else {}
    cand_build = cand_meta.get("build", {}) if isinstance(cand_meta.get("build"), dict) else {}
    cur_build = cur_meta.get("build", {}) if isinstance(cur_meta.get("build"), dict) else {}

    excel_report = read_json(EXCEL_IMPORT_REPORT_FILE, {})
    discovered_new_items = excel_report.get("discovered_new_items", []) if isinstance(excel_report, dict) else []

    return {
        "added_weapons": sorted(cand_weapon_slugs - cur_weapon_slugs),
        "removed_weapons": sorted(cur_weapon_slugs - cand_weapon_slugs),
        "added_classes": sorted(cand_class_slugs - cur_class_slugs),
        "removed_classes": sorted(cur_class_slugs - cand_class_slugs),
        "changed_talent_descriptions": changed_descriptions[:50],
        "changed_talent_description_count": len(changed_descriptions),
        "version": {"candidate": cand_build.get("version"), "current": cur_build.get("version")},
        "excel_version": {"candidate": cand_build.get("excel_version"), "current": cur_build.get("excel_version")},
        "wiki_degraded": bool(cand_build.get("wiki_degraded")),
        "hard_degraded": _has_hard_degradation(cand_meta),
        "version_alignment": _version_alignment(cand_meta),
        "modifier_changes": _build_modifier_changes(cand_meta, cur_meta),
        "excel_new_items": discovered_new_items,
        "excel_new_items_count": len(discovered_new_items),
    }


def build_diff_summary(candidate_dir: Path | None = None, current_dir: Path | None = None) -> dict[str, Any]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    current_root = current_dir or APP_DATA_DIR
    candidate_payloads = _load_payloads(candidate_root)
    current_payloads = _load_payloads(current_root)

    per_file = {filename: _diff_file(candidate_payloads[filename], current_payloads[filename]) for filename in RUNTIME_FILES}
    total_changed_files = sum(
        1
        for item in per_file.values()
        if item["added_count"] or item["removed_count"] or item["changed_count"]
    )
    return {
        "candidate_dir": candidate_root.as_posix(),
        "current_dir": current_root.as_posix(),
        "changed_file_count": total_changed_files,
        "files": per_file,
        "semantic_changes": _build_semantic_changes(candidate_payloads, current_payloads),
    }


def build_diff_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Runtime Candidate Diff",
        "",
        f"- Candidate Dir: `{summary['candidate_dir']}`",
        f"- Current Dir: `{summary['current_dir']}`",
        f"- Changed Files: `{summary['changed_file_count']}`",
        "",
    ]
    semantic = summary.get("semantic_changes", {}) if isinstance(summary.get("semantic_changes"), dict) else {}
    if semantic:
        alignment = semantic.get("version_alignment", {}) or {}
        lines.extend(
            [
                "## 语义变更",
                "",
                f"- 新增武器: {', '.join(semantic.get('added_weapons', [])) or '无'}",
                f"- 移除武器: {', '.join(semantic.get('removed_weapons', [])) or '无'}",
                f"- 新增职业: {', '.join(semantic.get('added_classes', [])) or '无'}",
                f"- 移除职业: {', '.join(semantic.get('removed_classes', [])) or '无'}",
                f"- 天赋描述变更: `{semantic.get('changed_talent_description_count', 0)}` 条",
                f"- 版本: candidate=`{semantic.get('version', {}).get('candidate')}` current=`{semantic.get('version', {}).get('current')}`",
                f"- Excel 版本: candidate=`{semantic.get('excel_version', {}).get('candidate')}` current=`{semantic.get('excel_version', {}).get('current')}`",
                f"- wiki 退化: `{semantic.get('wiki_degraded')}`",
                f"- 硬退化: `{semantic.get('hard_degraded')}`",
                f"- 版本对齐: `{alignment.get('aligned')}` ({alignment.get('reason')})",
                f"- Excel 待审新增项: `{semantic.get('excel_new_items_count', 0)}` 条",
            ]
        )
        modifiers = semantic.get("modifier_changes", {}) if isinstance(semantic.get("modifier_changes"), dict) else {}
        if modifiers:
            pos = modifiers.get("positive_modifier_pool", {}) or {}
            neg = modifiers.get("negative_modifier_pool", {}) or {}
            rules = modifiers.get("negative_modifier_rules", {}) or {}
            lines.extend(
                [
                    f"- 正向 modifier 变更: +`{pos.get('added_count', 0)}` -`{pos.get('removed_count', 0)}` ~`{pos.get('changed_count', 0)}`",
                    f"- 负向 modifier 变更: +`{neg.get('added_count', 0)}` -`{neg.get('removed_count', 0)}` ~`{neg.get('changed_count', 0)}`",
                    f"- 负向 modifier 规则路径变更: +`{rules.get('added_count', 0)}` -`{rules.get('removed_count', 0)}` ~`{rules.get('changed_count', 0)}`",
                ]
            )
        if semantic.get("excel_new_items"):
            lines.append("- 待审新增项明细:")
            for item in semantic["excel_new_items"][:20]:
                lines.append(f"  - `{item.get('slug')}` {item.get('excel_name')} ({item.get('source_sheet')})")
        lines.append("")
    for filename in RUNTIME_FILES:
        file_summary = summary["files"][filename]
        lines.extend(
            [
                f"## {filename}",
                "",
                f"- Added: `{file_summary['added_count']}`",
                f"- Removed: `{file_summary['removed_count']}`",
                f"- Changed: `{file_summary['changed_count']}`",
            ]
        )
        if file_summary["changed_paths"]:
            lines.append("- Changed Paths:")
            lines.extend(f"  - `{path}`" for path in file_summary["changed_paths"][:20])
        if file_summary["added_paths"]:
            lines.append("- Added Paths:")
            lines.extend(f"  - `{path}`" for path in file_summary["added_paths"][:10])
        if file_summary["removed_paths"]:
            lines.append("- Removed Paths:")
            lines.extend(f"  - `{path}`" for path in file_summary["removed_paths"][:10])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_diff_artifacts(candidate_dir: Path | None = None, current_dir: Path | None = None) -> dict[str, Any]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    summary = build_diff_summary(candidate_root, current_dir)
    write_json(candidate_root / DIFF_JSON_NAME, summary)
    (candidate_root / DIFF_MD_NAME).write_text(build_diff_markdown(summary), encoding="utf-8")
    return summary


def _validation_issue_count(candidate_root: Path) -> int:
    report = validate_runtime_data(candidate_root, VALIDATION_REPORT_FILE)
    return int(report.get("summary", {}).get("issue_count", 0) or 0)


def _extract_version_number(value: Any) -> str:
    """从版本字符串抽取数字段（如 'Hotfix 13.2' → '13.2'，'13.2' → '13.2'）。"""
    match = re.search(r"\d+(?:\.\d+)*", str(value or ""))
    return match.group(0) if match else ""


def _version_alignment(candidate_meta: dict[str, Any]) -> dict[str, Any]:
    """比较候选 meta 的 wiki version 与 excel version。

    aligned=True 版本一致；aligned=False 不一致（apply 需 --accept-version-mismatch）；
    aligned=None excel 版本缺失（无法判定，仅 warning 不阻断）。
    """
    build = candidate_meta.get("build", {}) if isinstance(candidate_meta, dict) else {}
    wiki_version = _extract_version_number(build.get("version"))
    excel_version = _extract_version_number(build.get("excel_version"))
    if not excel_version:
        return {"aligned": None, "wiki_version": wiki_version, "excel_version": excel_version, "reason": "excel_version_missing"}
    if wiki_version != excel_version:
        return {"aligned": False, "wiki_version": wiki_version, "excel_version": excel_version, "reason": "version_mismatch"}
    return {"aligned": True, "wiki_version": wiki_version, "excel_version": excel_version, "reason": "aligned"}


def _hard_degradation_reasons(candidate_meta: dict[str, Any]) -> list[str]:
    build = candidate_meta.get("build", {}) if isinstance(candidate_meta, dict) else {}
    degradation = build.get("degradation", {}) if isinstance(build.get("degradation"), dict) else {}
    reasons: list[str] = []
    if degradation.get("structure_degraded"):
        reasons.extend(str(item) for item in degradation.get("reasons", []) if str(item).strip())
        if not reasons:
            reasons.append("structure_degraded")
    if degradation.get("talent_degraded"):
        reasons.extend(str(item) for item in degradation.get("talent_reasons", []) if str(item).strip())
        if not any(str(reason).startswith("talent_") for reason in reasons):
            reasons.append("talent_degraded")
    return sorted(set(reasons))


def _has_hard_degradation(candidate_meta: dict[str, Any]) -> bool:
    return bool(_hard_degradation_reasons(candidate_meta))


def should_keep_candidate(candidate_dir: Path | None = None, current_dir: Path | None = None) -> dict[str, Any]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    current_root = current_dir or APP_DATA_DIR
    summary = build_diff_summary(candidate_root, current_root)
    validation_issue_count = _validation_issue_count(candidate_root)
    has_diff = int(summary.get("changed_file_count", 0) or 0) > 0
    candidate_meta = read_json(candidate_root / "meta.json", {})
    candidate_build = candidate_meta.get("build", {}) if isinstance(candidate_meta.get("build"), dict) else {}
    version_alignment = _version_alignment(candidate_meta)
    excel_report = read_json(EXCEL_IMPORT_REPORT_FILE, {})
    excel_new_items = excel_report.get("discovered_new_items", []) if isinstance(excel_report, dict) else []
    return {
        "candidate_dir": candidate_root.as_posix(),
        "current_dir": current_root.as_posix(),
        "validation_issue_count": validation_issue_count,
        "has_diff": has_diff,
        "version_alignment": version_alignment,
        "wiki_degraded": bool(candidate_build.get("wiki_degraded")),
        "hard_degraded": _has_hard_degradation(candidate_meta),
        "degradation": candidate_build.get("degradation", {}) if isinstance(candidate_build.get("degradation"), dict) else {},
        "excel_new_items_count": len(excel_new_items),
        "should_keep": validation_issue_count > 0 or has_diff,
    }


def apply_candidate(
    candidate_dir: Path | None = None,
    target_dir: Path | None = None,
    *,
    cleanup: bool = True,
    accept_version_mismatch: bool = False,
    accept_hard_degradation: bool = False,
) -> dict[str, str]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    app_root = target_dir or APP_DATA_DIR
    candidate_meta = read_json(candidate_root / "meta.json", {})
    alignment = _version_alignment(candidate_meta)
    if alignment["aligned"] is False and not accept_version_mismatch:
        raise RuntimeError(
            f"Version mismatch: wiki={alignment['wiki_version']} excel={alignment['excel_version']}; "
            "确认源数据版本后重试，或显式带 --accept-version-mismatch 应用。"
        )
    hard_reasons = _hard_degradation_reasons(candidate_meta)
    if hard_reasons and not accept_hard_degradation:
        raise RuntimeError(
            "Hard wiki degradation detected: "
            f"{', '.join(hard_reasons)}；修复 wiki 抓取后重试，或显式带 --accept-hard-degradation 应用。"
        )
    app_root.mkdir(parents=True, exist_ok=True)
    for filename in RUNTIME_FILES:
        source = candidate_root / filename
        if not source.exists():
            raise FileNotFoundError(f"Missing candidate runtime file: {source}")
        shutil.copy2(source, app_root / filename)
    if cleanup:
        clean_candidate(candidate_root)
    return {
        "candidate_dir": candidate_root.as_posix(),
        "target_dir": app_root.as_posix(),
        "status": "applied_and_cleaned" if cleanup else "applied",
        "version_alignment": alignment,
    }


def clean_candidate(candidate_dir: Path | None = None) -> dict[str, str]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    if candidate_root.exists():
        shutil.rmtree(candidate_root)
    return {"candidate_dir": candidate_root.as_posix(), "status": "cleaned"}


def maybe_clean_candidate(candidate_dir: Path | None = None, current_dir: Path | None = None) -> dict[str, str]:
    status = should_keep_candidate(candidate_dir, current_dir)
    candidate_root = Path(status["candidate_dir"])
    if status["should_keep"]:
        return {"candidate_dir": candidate_root.as_posix(), "status": "kept_candidate"}
    return clean_candidate(candidate_root)


def apply_or_clean_candidate(
    candidate_dir: Path | None = None,
    target_dir: Path | None = None,
    *,
    accept_version_mismatch: bool = False,
    accept_hard_degradation: bool = False,
) -> dict[str, str]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    app_root = target_dir or APP_DATA_DIR
    status = should_keep_candidate(candidate_root, app_root)
    if status["validation_issue_count"] > 0:
        raise RuntimeError("Candidate validation failed; refusing to apply or clean.")
    if not status["has_diff"]:
        return clean_candidate(candidate_root)
    return apply_candidate(
        candidate_root,
        app_root,
        accept_version_mismatch=accept_version_mismatch,
        accept_hard_degradation=accept_hard_degradation,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage candidate runtime outputs before they are applied to app/data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff_parser = subparsers.add_parser("diff-candidate", help="Create machine-readable and markdown diff summaries.")
    diff_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    diff_parser.add_argument("--current-dir", default=str(APP_DATA_DIR))

    apply_parser = subparsers.add_parser("apply-candidate", help="Apply candidate runtime files into app/data and clean up.")
    apply_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    apply_parser.add_argument("--target-dir", default=str(APP_DATA_DIR))
    apply_parser.add_argument("--keep-candidate", action="store_true", help="Apply runtime files but keep the candidate directory.")
    apply_parser.add_argument("--accept-version-mismatch", action="store_true", help="允许在 wiki 与 Excel 版本不一致时强制应用候选。")
    apply_parser.add_argument("--accept-hard-degradation", action="store_true", help="允许在 wiki 硬退化时强制应用候选。")

    clean_parser = subparsers.add_parser("clean-candidate", help="Remove candidate runtime files.")
    clean_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))

    maybe_clean_parser = subparsers.add_parser("maybe-clean-candidate", help="Clean candidate only when it matches current app/data and validation passes.")
    maybe_clean_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    maybe_clean_parser.add_argument("--current-dir", default=str(APP_DATA_DIR))

    status_parser = subparsers.add_parser("candidate-status", help="Report whether the candidate should be kept.")
    status_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    status_parser.add_argument("--current-dir", default=str(APP_DATA_DIR))

    apply_or_clean_parser = subparsers.add_parser("apply-or-clean-candidate", help="Apply changed candidate or clean it when unchanged.")
    apply_or_clean_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    apply_or_clean_parser.add_argument("--target-dir", default=str(APP_DATA_DIR))
    apply_or_clean_parser.add_argument("--accept-version-mismatch", action="store_true", help="允许在 wiki 与 Excel 版本不一致时强制应用候选。")
    apply_or_clean_parser.add_argument("--accept-hard-degradation", action="store_true", help="允许在 wiki 硬退化时强制应用候选。")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.command == "diff-candidate":
        write_diff_artifacts(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve())
    elif args.command == "apply-candidate":
        apply_candidate(
            Path(args.candidate_dir).resolve(),
            Path(args.target_dir).resolve(),
            cleanup=not args.keep_candidate,
            accept_version_mismatch=args.accept_version_mismatch,
            accept_hard_degradation=args.accept_hard_degradation,
        )
    elif args.command == "clean-candidate":
        clean_candidate(Path(args.candidate_dir).resolve())
    elif args.command == "maybe-clean-candidate":
        maybe_clean_candidate(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve())
    elif args.command == "candidate-status":
        print(__import__("json").dumps(should_keep_candidate(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve()), ensure_ascii=False, indent=2))
    elif args.command == "apply-or-clean-candidate":
        apply_or_clean_candidate(
            Path(args.candidate_dir).resolve(),
            Path(args.target_dir).resolve(),
            accept_version_mismatch=args.accept_version_mismatch,
            accept_hard_degradation=args.accept_hard_degradation,
        )
