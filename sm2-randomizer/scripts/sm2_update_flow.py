#!/usr/bin/env python
"""sm2-randomizer 标准数据更新流程入口。

人工把新 Excel 放进 sm2-randomizer 后，由 CC/Codex 跑本脚本：串起 Excel 导入、
（可选）wiki 增量抓取、候选构建、校验、diff，生成人审报告与终端摘要，**停在 apply 之前**。
确认后人工显式跑 apply-candidate + package-release 完成覆盖打包。

agent 无关：CC 与 CX 均可直接调用本命令。退出码：校验有问题→非0；否则0。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.compute.update_review import UPDATE_REVIEW_MD, build_update_review


def _run(command: list[str]) -> int:
    print(f"[update-flow] $ {' '.join(command)}")
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return int(completed.returncode)


def _step(label: str, command: list[str]) -> int:
    print(f"\n=== {label} ===")
    return _run(command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sm2-randomizer 标准数据更新流程：跑到变动清单即停，不 apply/不打包。")
    parser.add_argument("--skip-wiki", action="store_true", help="跳过 wiki 抓取，只用现有 wiki raw + 重跑 Excel。")
    parser.add_argument("--headless", action="store_true", help="wiki 资源抓取用 headless 浏览器。")
    parser.add_argument("--force-refresh", action="store_true", help="强制全量重抓 wiki 结构页，绕过 hash 增量。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    py = sys.executable

    # 1. Excel 导入
    if _step("Excel 导入", [py, "-m", "pipeline.collect.excel.run"]) != 0:
        print("[update-flow] Excel 导入失败，终止。")
        return 1

    # 2. wiki 抓取（可选 + 增量）
    if not args.skip_wiki:
        wiki_cmd = [py, "-m", "pipeline.collect.wiki.run"]
        if args.headless:
            wiki_cmd.append("--headless")
        if args.force_refresh:
            wiki_cmd.append("--force-refresh")
        if _step("wiki 抓取（增量）", wiki_cmd) != 0:
            print("[update-flow] wiki 抓取失败，终止。可加 --skip-wiki 仅用现有 wiki raw。")
            return 1
    else:
        print("\n=== wiki 抓取 ===\n[update-flow] 已跳过（--skip-wiki），使用现有 wiki raw。")

    # 3. 构建候选
    if _step("构建候选", [py, "-m", "pipeline.compute.build_runtime_data", "--output-dir", "pipeline/tmp_publish/"]) != 0:
        return 1

    # 4. 校验
    if _step("校验候选", [py, "-m", "pipeline.compute.validate_runtime_data", "--target-dir", "pipeline/tmp_publish/"]) != 0:
        return 1

    # 5. diff
    if _step("生成 diff", [py, "-m", "pipeline.compute.publish_candidate", "diff-candidate", "--candidate-dir", "pipeline/tmp_publish/"]) != 0:
        return 1

    # 6. 人审报告
    print("\n=== 生成人审报告 ===")
    summary = build_update_review()
    summary["wiki_skipped"] = args.skip_wiki
    _print_terminal_summary(summary)
    return 0 if summary.get("validation_issue_count", 0) == 0 else 1


def _print_terminal_summary(s: dict) -> None:
    excel = s.get("excel", {}) or {}
    inc = s.get("wiki_incremental", {}) or {}
    sem = s.get("semantic_changes", {}) or {}
    alignment = s.get("version_alignment", {}) or {}
    print("\n" + "=" * 60)
    print("[update-flow] 终端摘要")
    print("=" * 60)
    print(f"[update-flow] excel 导入: imported={excel.get('imported_count')} failures={excel.get('failure_count')} "
          f"discovered={len(excel.get('discovered_new_items', []))} greyed_excluded={len(excel.get('greyed_excluded', []))}")
    if s.get("wiki_skipped"):
        print(f"[update-flow] wiki: 已跳过(--skip-wiki) wiki_degraded={s.get('wiki_degraded')}(沿用上次 raw)")
    else:
        print(f"[update-flow] wiki: 增量跳过={inc.get('skipped_count')} 重抓={inc.get('refetched_count')} "
              f"wiki_degraded={s.get('wiki_degraded')}")
    print(f"[update-flow] 校验: issue_count={s.get('validation_issue_count')}")
    print(f"[update-flow] 版本对齐: aligned={alignment.get('aligned')} (wiki={alignment.get('wiki_version')} excel={alignment.get('excel_version')})")
    print(f"[update-flow] 变动: +{len(sem.get('added_weapons', []))}武器 -{len(sem.get('removed_weapons', []))}武器 "
          f"+{len(sem.get('added_classes', []))}职业 -{len(sem.get('removed_classes', []))}职业 "
          f"天赋描述变更{sem.get('changed_talent_description_count', 0)}条 "
          f"待审新增{sem.get('excel_new_items_count', 0)}条")
    print(f"[update-flow] 人审报告: {UPDATE_REVIEW_MD.relative_to(PROJECT_ROOT)}")
    print("[update-flow] 未 apply、未打包。")
    aligned = alignment.get("aligned")
    if s.get("validation_issue_count", 0) != 0:
        print("[update-flow] 校验有问题，请先排查 pipeline/store/reports/runtime/runtime_validation.json")
    elif aligned is False:
        print("[update-flow] 版本不齐，确认后: python build_release.py apply-candidate --accept-version-mismatch && python build_release.py package-release")
    else:
        print("[update-flow] 确认后: python build_release.py apply-candidate && python build_release.py package-release [--with-exe]")
    print("[update-flow] 不更新则: python build_release.py clean-candidate")
    print("=" * 60)


if __name__ == "__main__":
    raise SystemExit(main())
