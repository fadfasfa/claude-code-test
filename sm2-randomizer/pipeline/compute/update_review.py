from __future__ import annotations

"""人审变动报告生成入口。

复用 should_keep_candidate（含版本对齐/退化/待审项）与 build_diff_summary 的
semantic_changes，汇总 Excel 导入报告与 wiki 增量统计，输出一份分发者可读的
update_review.md，并返回终端摘要所需的结构化数据。供 scripts/sm2_update_flow.py 调用。
"""

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
        },
        "degradation": wiki_deg,
        "semantic_changes": semantic,
    }

    markdown = _render_markdown(summary)
    UPDATE_REVIEW_MD.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_REVIEW_MD.write_text(markdown, encoding="utf-8")
    write_json(UPDATE_REVIEW_JSON, summary)
    return summary


def _render_markdown(s: dict[str, Any]) -> str:
    alignment = s.get("version_alignment", {}) or {}
    excel = s.get("excel", {}) or {}
    inc = s.get("wiki_incremental", {}) or {}
    deg = s.get("degradation", {}) or {}
    sem = s.get("semantic_changes", {}) or {}
    issue_count = s.get("validation_issue_count", 0)
    aligned = alignment.get("aligned")
    hard_degraded = bool(s.get("hard_degraded"))
    can_apply = issue_count == 0 and aligned is not False and not hard_degraded

    lines = [
        "# sm2-randomizer 数据更新人审报告",
        "",
        "## 概览",
        "",
        f"- wiki 版本: `{s.get('wiki_version')}`",
        f"- Excel 版本: `{s.get('excel_version')}`",
        f"- 版本对齐: `{aligned}` ({alignment.get('reason')})",
        f"- wiki 本轮跳过: `{s.get('wiki_skipped')}`",
        f"- wiki 退化: `{s.get('wiki_degraded')}`",
        f"- wiki 硬退化: `{hard_degraded}`",
        f"- 校验问题数: `{issue_count}`",
        f"- 有变动: `{s.get('has_diff')}`",
        f"- 可安全 apply: `{can_apply}`",
        "",
        "## Excel 导入",
        "",
        f"- 导入武器数: `{excel.get('imported_count')}`",
        f"- 失败数: `{excel.get('failure_count')}`",
        f"- 灰色屏蔽词条数: `{len(excel.get('greyed_excluded', []))}`",
        f"- 待审新增项数: `{len(excel.get('discovered_new_items', []))}`",
        f"- 表头告警数: `{len(excel.get('header_warnings', []))}`",
    ]
    if excel.get("greyed_excluded"):
        lines.append("- 灰色屏蔽词条:")
        lines.extend(f"  - `{t}`" for t in excel["greyed_excluded"])
    if excel.get("discovered_new_items"):
        lines.append("- 待审新增项:")
        for item in excel["discovered_new_items"][:20]:
            lines.append(f"  - `{item.get('slug')}` {item.get('excel_name')} ({item.get('source_sheet')})")
    if excel.get("header_warnings"):
        lines.append("- 表头告警:")
        for w in excel["header_warnings"][:10]:
            lines.append(f"  - `{w.get('code')}` {w.get('sheet')}: {w.get('message')}")

    lines.extend([
        "",
        "## wiki 抓取",
        "",
        f"- 本轮跳过 wiki: `{s.get('wiki_skipped')}`",
        f"- 增量跳过页数: `{inc.get('skipped_count')}`",
        f"- 重新抓取页数: `{inc.get('refetched_count')}`",
        f"- 强制刷新: `{inc.get('force_refresh')}`",
        f"- 结构退化: `{deg.get('structure_degraded')}`",
        f"- 天赋退化: `{deg.get('talent_degraded')}`",
        f"- 软退化: `{deg.get('soft_degraded')}`",
    ])
    if deg.get("reasons"):
        lines.append("- 硬退化原因:")
        lines.extend(f"  - `{r}`" for r in deg["reasons"])
    if deg.get("soft_reasons"):
        lines.append("- 软退化原因:")
        lines.extend(f"  - `{r}`" for r in deg["soft_reasons"])
    if deg.get("talent_reasons"):
        lines.append("- 天赋退化原因:")
        lines.extend(f"  - `{r}`" for r in deg["talent_reasons"])
    if s.get("wiki_skipped"):
        lines.append("- 说明：本轮使用既有 wiki raw，以上 wiki 增量与退化信息不代表本轮重新抓取结果。")

    lines.extend([
        "",
        "## 语义变更",
        "",
        f"- 新增武器: {', '.join(sem.get('added_weapons', [])) or '无'}",
        f"- 移除武器: {', '.join(sem.get('removed_weapons', [])) or '无'}",
        f"- 新增职业: {', '.join(sem.get('added_classes', [])) or '无'}",
        f"- 移除职业: {', '.join(sem.get('removed_classes', [])) or '无'}",
        f"- 天赋描述变更: `{sem.get('changed_talent_description_count', 0)}` 条",
        f"- Excel 待审新增项: `{sem.get('excel_new_items_count', 0)}` 条",
    ])
    modifiers = sem.get("modifier_changes", {}) if isinstance(sem.get("modifier_changes"), dict) else {}
    if modifiers:
        pos = modifiers.get("positive_modifier_pool", {}) or {}
        neg = modifiers.get("negative_modifier_pool", {}) or {}
        rules = modifiers.get("negative_modifier_rules", {}) or {}
        lines.extend([
            f"- 正向 modifier 变更: +`{pos.get('added_count', 0)}` -`{pos.get('removed_count', 0)}` ~`{pos.get('changed_count', 0)}`",
            f"- 负向 modifier 变更: +`{neg.get('added_count', 0)}` -`{neg.get('removed_count', 0)}` ~`{neg.get('changed_count', 0)}`",
            f"- 负向 modifier 规则路径变更: +`{rules.get('added_count', 0)}` -`{rules.get('removed_count', 0)}` ~`{rules.get('changed_count', 0)}`",
        ])

    lines.extend(["", "## 结论与下一步", ""])
    if issue_count > 0:
        lines.append(f"- ❌ 校验有 `{issue_count}` 个问题，**不可 apply**，请先排查 runtime_validation.json。")
    elif aligned is False or hard_degraded:
        command_flags = []
        if aligned is False:
            command_flags.append("--accept-version-mismatch")
            lines.append(f"- ⚠️ 版本不齐 (wiki={alignment.get('wiki_version')} excel={alignment.get('excel_version')})。")
        if hard_degraded:
            command_flags.append("--accept-hard-degradation")
            lines.append("- ⚠️ wiki 硬退化存在，确认后才可显式强制 apply/package。")
        lines.append("- 确认后可强制 apply：")
        lines.append("  ```")
        flags = " ".join(command_flags)
        lines.append(f"  python build_release.py apply-candidate {flags}".rstrip())
        lines.append(f"  python build_release.py package-release {flags} [--with-exe]".rstrip())
        lines.append("  ```")
    else:
        lines.append("- ✅ 校验通过、版本对齐，可安全 apply：")
        lines.append("  ```")
        lines.append("  python build_release.py apply-candidate")
        lines.append("  python build_release.py package-release [--with-exe]")
        lines.append("  ```")
    if s.get("wiki_degraded"):
        lines.append("- ⚠️ wiki 处于退化状态，已降级字段并保计数，可出包但建议排查 wiki 抓取（见上方退化原因）。")
    lines.append("- 若不更新：`python build_release.py clean-candidate`")
    lines.append("")
    return "\n".join(lines)
