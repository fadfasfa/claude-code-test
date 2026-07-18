"""开发自检与手动验收入口。

自动化断言由 pytest 直接收集；本模块负责 CLI 参数、bundle
manifest、健康摘要和 Web 联动人工验收等非 pytest 模式。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import hextech.modules.data.catalog.runtime_store as runtime_store
from tooling.checks import manual as dev_check_manual


DATA_EVIDENCE_DIR = RUN_DIR / "resources" / "evidence" / "mayhem"
check_bundle_manifest = dev_check_manual.check_bundle_manifest


def build_hextech_scrape_health_summary() -> dict[str, Any]:
    """构建来源健康摘要。"""

    return dev_check_manual.build_hextech_scrape_health_summary(
        runtime_store_module=runtime_store,
        data_evidence_dir=DATA_EVIDENCE_DIR,
    )


def print_hextech_scrape_health_summary(*, as_json: bool) -> None:
    """输出来源健康摘要。"""

    dev_check_manual.print_hextech_scrape_health_summary(
        as_json=as_json,
        runtime_store_module=runtime_store,
        data_evidence_dir=DATA_EVIDENCE_DIR,
    )


def run_manual_web_synergy(args: argparse.Namespace) -> dict[str, Any]:
    """把手动 Web 验收委托给专用模块。"""

    return dev_check_manual.run_manual_web_synergy(args)


def run_pytest_gate(*, overlay_only: bool = False, deep: bool = False) -> int:
    """把自动化门禁委托给 pytest。"""

    marker_parts = ["dev_gate"]
    if overlay_only:
        marker_parts.append("overlay")
    if not deep:
        marker_parts.append("not deep")
    marker = " and ".join(marker_parts)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", marker],
        cwd=str(RUN_DIR),
        check=False,
    )
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech 开发自检与手动验收入口。")
    parser.add_argument("--bundle-manifest", action="store_true", help="只输出并校验 bundle manifest 明细。")
    parser.add_argument("--overlay-only", action="store_true", help="只执行游戏内 overlay 相关离线自检。")
    parser.add_argument("--deep", action="store_true", help="包含慢速 overlay sidecar/cache 等深度自检。")
    parser.add_argument("--hextech-health", action="store_true", help="只读输出海克斯胜率/联动抓取健康摘要。")
    parser.add_argument("--json", action="store_true", help="与 --hextech-health 一起使用时输出 JSON。")
    parser.add_argument("--manual-web-synergy", action="store_true", help="执行 Web/UI 详情页联动人工验收辅助检查。")
    parser.add_argument("--base-url", default="", help="本地 Web 地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--port-file", default="", help="UI/Web 写出的 web_server_port.txt；未传 base-url 时使用。")
    parser.add_argument("--source-base", default="https://apexlol.info/zh")
    parser.add_argument("--seed", type=int, default=20260518)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--first-n", type=int, default=5)
    parser.add_argument("--label", default="web", help="输出标签，例如 web 或 ui。")
    parser.add_argument("--screenshot-dir", default=os.path.join("var", "reports", "acceptance", "synergy"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.manual_web_synergy:
        result = run_manual_web_synergy(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    elif args.bundle_manifest:
        try:
            check_bundle_manifest(verbose=True)
        except Exception as exc:
            print(f"bundle manifest 校验失败：{exc}", file=sys.stderr)
            return 1
        return 0
    elif args.overlay_only:
        exit_code = run_pytest_gate(overlay_only=True, deep=bool(args.deep))
        if exit_code == 0:
            print("overlay 深度自检通过。" if args.deep else "overlay fast 自检通过。")
        return exit_code
    elif args.hextech_health:
        print_hextech_scrape_health_summary(as_json=bool(args.json))
        return 0
    exit_code = run_pytest_gate(deep=bool(args.deep))
    if exit_code == 0:
        print("所有开发深度自检通过。" if args.deep else "所有开发 fast 自检通过。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
