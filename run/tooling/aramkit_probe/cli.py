"""ARAMKit 独立抓取探针命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys

from .core import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, FetchConfig, ProbeError, compare_latest_runs, run_fetch


def _concurrency(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("并发必须是整数") from exc
    if not 1 <= parsed <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(f"并发必须位于 1..{MAX_CONCURRENCY}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立抓取并验证 ARAMKit 全英雄统计，不接入正式数据链路。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="执行一次全量抓取并生成留证快照。")
    fetch.add_argument("--dataset", choices=("all", "high"), default="all", help="样本范围，默认 all。")
    fetch.add_argument(
        "--concurrency",
        type=_concurrency,
        default=DEFAULT_CONCURRENCY,
        help=f"详情并发，默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY}。",
    )
    fetch.add_argument("--version", default="latest", help="latest 或 data/versions.json 中公开的版本号。")

    compare = subparsers.add_parser("compare", help="比较最近两次完整抓取。")
    compare.add_argument("--dataset", choices=("all", "high"), default="all", help="要比较的数据集。")
    compare.add_argument("--latest", type=int, choices=(2,), default=2, help="固定比较最近两次完整 run。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "fetch":
            result = run_fetch(
                FetchConfig(dataset=args.dataset, concurrency=args.concurrency, version=args.version)
            )
            summary = {
                "complete": result.complete,
                "runDir": str(result.run_dir),
                "manifest": str(result.manifest_path),
                "dataset": result.manifest.get("dataset"),
                "version": result.manifest.get("version", {}).get("version"),
                "durationSeconds": result.manifest.get("durationSeconds"),
                "requests": result.manifest.get("requests"),
                "coverage": result.manifest.get("coverage"),
                "errors": result.manifest.get("errors"),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if result.complete else 1

        report = compare_latest_runs(dataset=args.dataset, latest=args.latest)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    except (ProbeError, OSError, ValueError) as exc:
        print(json.dumps({"complete": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]
