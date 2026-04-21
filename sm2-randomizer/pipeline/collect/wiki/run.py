from __future__ import annotations

"""Wiki 采集统一入口。

负责按固定顺序编排结构抓取与资源抓取，支持按参数跳过子步骤。
输入为命令行参数，输出为对应子脚本的退出码。
"""

import argparse
import subprocess
import sys
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent
SCRAPE_WIKI = CURRENT_DIR / "scrape_wiki.py"
SCRAPE_PERKS = CURRENT_DIR / "scrape_perks.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run wiki collection steps through the unified Python entrypoint.")
    parser.add_argument("--skip-structure", action="store_true", help="Skip the structure scrape step.")
    parser.add_argument("--skip-assets", action="store_true", help="Skip the class/perk asset refresh step.")
    parser.add_argument("--headless", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--dump-dom", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--force-download", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--class", dest="class_titles", action="append", default=[], help="Pass through to scrape_perks.py.")
    return parser.parse_args()


def _run(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=CURRENT_DIR, check=False)
    return int(completed.returncode)


def main() -> int:
    args = parse_args()
    if not args.skip_structure:
        exit_code = _run([sys.executable, str(SCRAPE_WIKI)])
        if exit_code != 0:
            return exit_code

    if not args.skip_assets:
        command = [sys.executable, str(SCRAPE_PERKS)]
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
