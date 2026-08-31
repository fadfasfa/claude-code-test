from __future__ import annotations

"""Wiki 采集统一入口。

负责按固定顺序编排结构抓取与资源抓取，支持按参数跳过子步骤。
输入为命令行参数，输出为对应子脚本的退出码。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent
SCRAPE_WIKI = CURRENT_DIR / "scrape_wiki.py"
SCRAPE_PERKS = CURRENT_DIR / "scrape_perks.py"
RAW_DATA_FILE = CURRENT_DIR.parents[1] / "store" / "raw" / "wiki" / "原始抓取数据.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run wiki collection steps through the unified Python entrypoint.")
    parser.add_argument("--skip-structure", action="store_true", help="Skip the structure scrape step.")
    parser.add_argument("--skip-assets", action="store_true", help="Skip the class/perk asset refresh step.")
    parser.add_argument("--headless", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--dump-dom", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--force-download", action="store_true", help="Pass through to scrape_perks.py.")
    parser.add_argument("--force-refresh", action="store_true", help="强制全量重抓 wiki 结构页，绕过页面 hash 增量。")
    parser.add_argument("--class", dest="class_titles", action="append", default=[], help="Pass through to scrape_perks.py.")
    return parser.parse_args()


def _run(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=CURRENT_DIR, check=False)
    return int(completed.returncode)


def _load_changed_class_pages() -> list[str]:
    if not RAW_DATA_FILE.exists():
        raise RuntimeError(f"Wiki raw data missing after structure scrape: {RAW_DATA_FILE}")
    try:
        payload = json.loads(RAW_DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to read wiki incremental metadata: {exc}") from exc
    meta = payload.get("meta") if isinstance(payload, dict) else None
    incremental = meta.get("incremental") if isinstance(meta, dict) else None
    changed = incremental.get("changed_class_pages") if isinstance(incremental, dict) else None
    if not isinstance(changed, list) or not all(isinstance(item, str) and item.strip() for item in changed):
        raise RuntimeError("Wiki raw data missing meta.incremental.changed_class_pages.")
    return list(dict.fromkeys(item.strip() for item in changed))


def resolve_asset_refresh_classes(args: argparse.Namespace) -> list[str] | None:
    """Return explicit class titles, [] to skip assets, or None to refresh all."""
    if args.class_titles:
        return list(dict.fromkeys(args.class_titles))
    if args.force_download or args.skip_structure:
        return None
    return _load_changed_class_pages()


def main() -> int:
    args = parse_args()
    if not args.skip_structure:
        command = [sys.executable, str(SCRAPE_WIKI)]
        if args.force_refresh:
            command.append("--force-refresh")
        exit_code = _run(command)
        if exit_code != 0:
            return exit_code

    if not args.skip_assets:
        selected_classes = resolve_asset_refresh_classes(args)
        if selected_classes == []:
            print("[sm2-randomizer] Wiki class pages unchanged; skipping talent/class asset refresh.")
            return 0
        command = [sys.executable, str(SCRAPE_PERKS)]
        if args.headless:
            command.append("--headless")
        if args.dump_dom:
            command.append("--dump-dom")
        if args.force_download:
            command.append("--force-download")
        if selected_classes is not None:
            for class_title in selected_classes:
                command.extend(["--class", class_title])
        exit_code = _run(command)
        if exit_code != 0:
            return exit_code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
