from __future__ import annotations

"""生成人工审查使用的前端消费数据变更清单。

版本对齐、退化、校验和抓取统计继续保留在内部 summary 供流程控制；人审 Markdown
与 JSON 只输出职业、武器池、天赋、策略词条和策略规则的实际前端数据变化。
"""

import json
from pathlib import Path
from typing import Any

from pipeline.common import (
    APP_DATA_DIR,
    EXCEL_IMPORT_REPORT_FILE,
    PIPELINE_STORE_REPORTS_SOURCE_DIR,
    PIPELINE_TMP_PUBLISH_DIR,
    WIKI_RAW_FILE,
    read_json,
    write_json,
)
from pipeline.compute.publish_candidate import build_diff_summary, should_keep_candidate

UPDATE_REVIEW_MD = PIPELINE_STORE_REPORTS_SOURCE_DIR / "update_review.md"
UPDATE_REVIEW_JSON = PIPELINE_STORE_REPORTS_SOURCE_DIR / "update_review.json"


def _load_excel_report() -> dict[str, Any]:
    data = read_json(EXCEL_IMPORT_REPORT_FILE, {})
    return data if isinstance(data, dict) else {}


def _load_wiki_incremental() -> dict[str, Any]:
    raw = read_json(WIKI_RAW_FILE, {})
    meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
    return meta.get("incremental", {}) if isinstance(meta.get("incremental"), dict) else {}


def _load_wiki_degradation() -> dict[str, Any]:
    raw = read_json(WIKI_RAW_FILE, {})
    meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
    return meta.get("degradation", {}) if isinstance(meta.get("degradation"), dict) else {}


def build_update_review(
    candidate_dir: Path | None = None,
    current_dir: Path | None = None,
    *,
    wiki_skipped: bool = False,
) -> dict[str, Any]:
    """构建人审变动报告，写 update_review.md，返回终端摘要所需数据。"""
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    current_root = current_dir or APP_DATA_DIR
    status = should_keep_candidate(candidate_root, current_root)
    diff = build_diff_summary(candidate_root, current_root)
    semantic = diff.get("semantic_changes", {}) if isinstance(diff.get("semantic_changes"), dict) else {}
    excel_report = _load_excel_report()
    wiki_inc = _load_wiki_incremental()
    wiki_deg = _load_wiki_degradation()

    alignment = status.get("version_alignment", {}) or {}
    candidate_meta = read_json(candidate_root / "meta.json", {})
    build = candidate_meta.get("build", {}) if isinstance(candidate_meta.get("build"), dict) else {}

    summary = {
        "excel_version": build.get("excel_version"),
        "wiki_version": build.get("version"),
        "wiki_degraded": status.get("wiki_degraded", False),
        "hard_degraded": status.get("hard_degraded", False),
        "wiki_skipped": wiki_skipped,
        "version_alignment": alignment,
        "validation_issue_count": status.get("validation_issue_count", 0),
        "has_diff": status.get("has_diff", False),
        "excel": {
            "imported_count": excel_report.get("imported_count", 0),
            "failure_count": excel_report.get("failure_count", 0),
            "discovered_new_items": excel_report.get("discovered_new_items", []),
            "greyed_excluded": excel_report.get("greyed_excluded", []),
            "header_warnings": excel_report.get("header_warnings", []),
        },
        "wiki_incremental": {
            "skipped_count": wiki_inc.get("skipped_count", 0),
            "refetched_count": wiki_inc.get("refetched_count", 0),
            "force_refresh": wiki_inc.get("force_refresh", False),
            "changed_class_pages": wiki_inc.get("changed_class_pages", []),
        },
        "degradation": wiki_deg,
        "semantic_changes": semantic,
    }

    markdown = _render_markdown(summary)
    UPDATE_REVIEW_MD.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_REVIEW_MD.write_text(markdown, encoding="utf-8")
    write_json(
        UPDATE_REVIEW_JSON,
        {"frontend_changes": semantic.get("frontend_changes", {})},
    )
    return summary


_FIELD_LABELS = {
    "name": "名称",
    "label": "标题",
    "detail": "说明",
    "description": "描述",
    "image_path": "图片",
    "icon_path": "图标",
    "col": "天赋列",
    "row": "天赋行",
    "risk_level": "风险等级",
    "core_tags": "标签",
    "aliases": "别名",
    "slot_type": "武器槽位",
}


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return f"“{value}”"
    return f"`{json.dumps(value, ensure_ascii=False, sort_keys=True)}`"


def _format_identity(value: Any) -> str:
    if isinstance(value, list):
        return " / ".join(str(item) for item in value)
    return str(value)


def _change_count(section: dict[str, Any]) -> int:
    return sum(len(section.get(key, [])) for key in ("added", "removed", "changed"))


def _append_record_changes(lines: list[str], title: str, section: dict[str, Any]) -> None:
    if not _change_count(section):
        return
    lines.extend(["", f"## {title}", ""])
    for item in section.get("added", []):
        lines.append(f"- 新增：{item.get('label')}（`{_format_identity(item.get('id'))}`）")
        value = item.get("value", {})
        if isinstance(value, dict):
            for field_name in ("detail", "description"):
                if value.get(field_name):
                    lines.append(f"  - {_FIELD_LABELS[field_name]}：{_format_value(value[field_name])}")
    for item in section.get("removed", []):
        lines.append(f"- 移除：{item.get('label')}（`{_format_identity(item.get('id'))}`）")
        value = item.get("value", {})
        if isinstance(value, dict):
            for field_name in ("detail", "description"):
                if value.get(field_name):
                    lines.append(f"  - 原{_FIELD_LABELS[field_name]}：{_format_value(value[field_name])}")
    for item in section.get("changed", []):
        lines.append(f"- 修改：{item.get('label')}（`{_format_identity(item.get('id'))}`）")
        for field in item.get("fields", []):
            field_name = str(field.get("field", ""))
            display_name = _FIELD_LABELS.get(field_name, field_name)
            lines.append(
                f"  - {display_name}：{_format_value(field.get('before'))} → {_format_value(field.get('after'))}"
            )


def _render_markdown(s: dict[str, Any]) -> str:
    sem = s.get("semantic_changes", {}) or {}
    frontend = sem.get("frontend_changes", {}) if isinstance(sem, dict) else {}
    frontend = frontend if isinstance(frontend, dict) else {}
    classes = frontend.get("classes", {}) or {}
    loadouts = frontend.get("loadouts", {}) or {}
    talents = frontend.get("talents", {}) or {}
    positive = frontend.get("positive_modifiers", {}) or {}
    negative = frontend.get("negative_modifiers", {}) or {}
    rules = frontend.get("modifier_rules", {}) or {}
    aliases = rules.get("title_aliases", {}) if isinstance(rules, dict) else {}
    aliases = aliases if isinstance(aliases, dict) else {}
    visible_replacements = [
        item for item in aliases.get("replaced", []) if not item.get("mirrors_modifier_detail")
    ]
    rule_count = len(rules.get("changed_fields", [])) + sum(
        len(aliases.get(key, [])) for key in ("added", "removed", "changed")
    ) + len(visible_replacements)

    lines = [
        "# SM2 前端数据变更清单",
        "",
        "本清单只列出随机抽取器前端实际消费的数据变化；抓取页数、文件路径、构建时间和实现细节不在此列。",
        "",
        "## 汇总",
        "",
        f"- 职业：`{_change_count(classes)}` 项",
        f"- 职业武器池：`{_change_count(loadouts)}` 项",
        f"- 天赋：`{_change_count(talents)}` 项",
        f"- 正向策略词条：`{_change_count(positive)}` 项",
        f"- 负向策略词条：`{_change_count(negative)}` 项",
        f"- 策略规则：`{rule_count}` 项",
    ]
    if not frontend.get("has_changes"):
        lines.extend(["", "本次前端消费数据无变化。"])
        return "\n".join(lines) + "\n"

    _append_record_changes(lines, "负向策略词条", negative)
    _append_record_changes(lines, "正向策略词条", positive)
    _append_record_changes(lines, "职业", classes)
    _append_record_changes(lines, "职业武器池", loadouts)
    _append_record_changes(lines, "天赋", talents)

    if rule_count:
        lines.extend(["", "## 策略规则", ""])
        for field in rules.get("changed_fields", []):
            field_name = str(field.get("field", ""))
            display_name = {"exact_conflicts": "冲突规则", "quota_limits": "配额规则"}.get(field_name, field_name)
            lines.append(
                f"- {display_name}：{_format_value(field.get('before'))} → {_format_value(field.get('after'))}"
            )
        for item in visible_replacements:
            lines.append(
                f"- 识别文本更新（`{item.get('target')}`）：{_format_value(item.get('before'))} → {_format_value(item.get('after'))}"
            )
        for item in aliases.get("added", []):
            lines.append(f"- 新增识别文本：{_format_value(item.get('alias'))} → `{item.get('target')}`")
        for item in aliases.get("removed", []):
            lines.append(f"- 移除识别文本：{_format_value(item.get('alias'))} → `{item.get('target')}`")
        for item in aliases.get("changed", []):
            lines.append(
                f"- 识别文本映射修改：{_format_value(item.get('alias'))}，`{item.get('before')}` → `{item.get('after')}`"
            )

    return "\n".join(lines) + "\n"
