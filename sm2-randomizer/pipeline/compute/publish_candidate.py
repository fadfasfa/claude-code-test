from __future__ import annotations

"""候选运行数据发布控制入口。

负责候选与当前运行数据的差异生成、应用/清理决策及候选目录生命周期管理。
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import APP_DATA_DIR, PIPELINE_TMP_PUBLISH_DIR, VALIDATION_REPORT_FILE, read_json, write_json
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


def should_keep_candidate(candidate_dir: Path | None = None, current_dir: Path | None = None) -> dict[str, Any]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    current_root = current_dir or APP_DATA_DIR
    summary = build_diff_summary(candidate_root, current_root)
    validation_issue_count = _validation_issue_count(candidate_root)
    has_diff = int(summary.get("changed_file_count", 0) or 0) > 0
    return {
        "candidate_dir": candidate_root.as_posix(),
        "current_dir": current_root.as_posix(),
        "validation_issue_count": validation_issue_count,
        "has_diff": has_diff,
        "should_keep": validation_issue_count > 0 or has_diff,
    }


def apply_candidate(candidate_dir: Path | None = None, target_dir: Path | None = None, *, cleanup: bool = True) -> dict[str, str]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    app_root = target_dir or APP_DATA_DIR
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


def apply_or_clean_candidate(candidate_dir: Path | None = None, target_dir: Path | None = None) -> dict[str, str]:
    candidate_root = candidate_dir or PIPELINE_TMP_PUBLISH_DIR
    app_root = target_dir or APP_DATA_DIR
    status = should_keep_candidate(candidate_root, app_root)
    if status["validation_issue_count"] > 0:
        raise RuntimeError("Candidate validation failed; refusing to apply or clean.")
    if not status["has_diff"]:
        return clean_candidate(candidate_root)
    return apply_candidate(candidate_root, app_root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage candidate runtime outputs before they are applied to app/data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff_parser = subparsers.add_parser("diff-candidate", help="Create machine-readable and markdown diff summaries.")
    diff_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    diff_parser.add_argument("--current-dir", default=str(APP_DATA_DIR))

    apply_parser = subparsers.add_parser("apply-candidate", help="Apply candidate runtime files into app/data and clean up.")
    apply_parser.add_argument("--candidate-dir", default=str(PIPELINE_TMP_PUBLISH_DIR))
    apply_parser.add_argument("--target-dir", default=str(APP_DATA_DIR))

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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.command == "diff-candidate":
        write_diff_artifacts(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve())
    elif args.command == "apply-candidate":
        apply_candidate(Path(args.candidate_dir).resolve(), Path(args.target_dir).resolve())
    elif args.command == "clean-candidate":
        clean_candidate(Path(args.candidate_dir).resolve())
    elif args.command == "maybe-clean-candidate":
        maybe_clean_candidate(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve())
    elif args.command == "candidate-status":
        print(__import__("json").dumps(should_keep_candidate(Path(args.candidate_dir).resolve(), Path(args.current_dir).resolve()), ensure_ascii=False, indent=2))
    elif args.command == "apply-or-clean-candidate":
        apply_or_clean_candidate(Path(args.candidate_dir).resolve(), Path(args.target_dir).resolve())
