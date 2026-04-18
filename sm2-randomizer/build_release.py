from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pipeline.common import PIPELINE_TMP_PUBLISH_DIR, VALIDATION_REPORT_FILE

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Top-level entrypoint for data refresh today and release packaging later.")
    subparsers = parser.add_subparsers(dest="command")

    refresh = subparsers.add_parser("refresh-data", help="Refresh pipeline data, build candidate runtime files, and emit diffs.")
    refresh.add_argument("--skip-wiki", action="store_true", help="Skip wiki collection.")
    refresh.add_argument("--skip-excel", action="store_true", help="Skip excel conversion.")
    refresh.add_argument("--skip-validate", action="store_true", help="Skip runtime validation.")
    refresh.add_argument("--skip-diff", action="store_true", help="Skip candidate diff artifact generation.")
    refresh.add_argument("--headless", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--dump-dom", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--force-download", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--class", dest="class_titles", action="append", default=[], help="Pass through to wiki asset refresh.")

    build_candidate = subparsers.add_parser("build-candidate", help="Build app-format runtime candidate files into pipeline/tmp_publish.")
    build_candidate.add_argument("--skip-validate", action="store_true", help="Skip runtime validation for the candidate output.")

    subparsers.add_parser("diff-candidate", help="Generate JSON and Markdown diff summaries for the current candidate.")
    subparsers.add_parser("apply-candidate", help="Apply reviewed candidate runtime files into app/data.")
    subparsers.add_parser("clean-candidate", help="Delete candidate runtime files.")

    package_release = subparsers.add_parser("package-release", help="Future release packaging hook.")
    package_release.add_argument("--skip-refresh", action="store_true", help="Skip data refresh before the packaging placeholder.")

    argv = sys.argv[1:]
    if not argv:
        argv = ["refresh-data"]
    return parser.parse_args(argv)


def _run(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return int(completed.returncode)


def refresh_data(args: argparse.Namespace) -> int:
    if not args.skip_wiki:
        command = [sys.executable, "-m", "pipeline.collect.wiki.run"]
        if args.headless:
            command.append("--headless")
        if args.dump_dom:
            command.append("--dump-dom")
        if args.force_download:
            command.append("--force-download")
        for class_title in args.class_titles:
            command.extend(["--class", class_title])
        exit_code = _run(command)
        if exit_code != 0:
            return exit_code

    if not args.skip_excel:
        exit_code = _run([sys.executable, "-m", "pipeline.collect.excel.run"])
        if exit_code != 0:
            return exit_code

    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.build_runtime_data",
            "--output-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )
    if exit_code != 0:
        return exit_code

    if not args.skip_validate:
        exit_code = _run(
            [
                sys.executable,
                "-m",
                "pipeline.compute.validate_runtime_data",
                "--target-dir",
                str(PIPELINE_TMP_PUBLISH_DIR),
                "--report-path",
                str(VALIDATION_REPORT_FILE),
            ]
        )
        if exit_code != 0:
            return exit_code

    if args.skip_diff:
        return 0

    return diff_candidate()


def build_candidate(skip_validate: bool = False) -> int:
    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.build_runtime_data",
            "--output-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )
    if exit_code != 0:
        return exit_code

    if skip_validate:
        return 0

    return _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.validate_runtime_data",
            "--target-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
            "--report-path",
            str(VALIDATION_REPORT_FILE),
        ]
    )


def diff_candidate() -> int:
    return _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.publish_candidate",
            "diff-candidate",
            "--candidate-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )


def apply_candidate() -> int:
    missing = [name for name in ("classes.json", "talents.json", "meta.json") if not (PIPELINE_TMP_PUBLISH_DIR / name).exists()]
    if missing:
        print(f"[sm2-randomizer] Missing candidate files in {PIPELINE_TMP_PUBLISH_DIR}: {', '.join(missing)}")
        return 1
    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.validate_runtime_data",
            "--target-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
            "--report-path",
            str(VALIDATION_REPORT_FILE),
        ]
    )
    if exit_code != 0:
        return exit_code
    validation = json.loads(VALIDATION_REPORT_FILE.read_text(encoding="utf-8")) if VALIDATION_REPORT_FILE.exists() else {}
    issue_count = validation.get("summary", {}).get("issue_count", 0)
    if issue_count:
        print(f"[sm2-randomizer] Candidate validation still has {issue_count} issues. Refusing to apply.")
        return 1
    return _run([sys.executable, "-m", "pipeline.compute.publish_candidate", "apply-candidate"])


def clean_candidate() -> int:
    return _run([sys.executable, "-m", "pipeline.compute.publish_candidate", "clean-candidate"])


def package_release(_: argparse.Namespace) -> int:
    print("[sm2-randomizer] Release packaging skeleton is in place, but the actual packaging chain is not implemented in this round.")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "package-release":
        if not args.skip_refresh:
            refresh_args = argparse.Namespace(
                skip_wiki=False,
                skip_excel=False,
                skip_validate=False,
                skip_diff=False,
                headless=False,
                dump_dom=False,
                force_download=False,
                class_titles=[],
            )
            exit_code = refresh_data(refresh_args)
            if exit_code != 0:
                return exit_code
        return package_release(args)
    if args.command == "build-candidate":
        return build_candidate(skip_validate=args.skip_validate)
    if args.command == "diff-candidate":
        return diff_candidate()
    if args.command == "apply-candidate":
        return apply_candidate()
    if args.command == "clean-candidate":
        return clean_candidate()
    return refresh_data(args)


if __name__ == "__main__":
    raise SystemExit(main())
