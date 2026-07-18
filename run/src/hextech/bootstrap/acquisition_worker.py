"""单一来源的隔离抓取 worker 入口。

本进程只写 immutable run、报告和 candidate pointer，不得直接切正式 current。
DataService coordinator 在所有来源都通过后，才通过 promotion journal 提升 cohort。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json


def _watch_cancel(path: Path, event: threading.Event) -> None:
    while not event.wait(0.1):
        if path.exists():
            event.set()
            return


def _load_pointer(path: Path, source: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise RuntimeError(f"{source} candidate pointer 无效")
    if source != "catalog" and payload.get("source") != source:
        raise RuntimeError(f"{source} candidate pointer 来源错位")
    return payload


def run_worker(
    source: str,
    *,
    force: bool,
    pointer_output: Path,
    cancel_file: Path,
    catalog_pointer: Path | None = None,
) -> dict[str, Any]:
    stop_event = threading.Event()
    threading.Thread(target=_watch_cancel, args=(cancel_file, stop_event), daemon=True).start()
    if catalog_pointer is not None:
        os.environ["HEXTECH_CATALOG_POINTER_PATH"] = os.fspath(catalog_pointer)

    started = time.monotonic()
    result: Mapping[str, Any] | bool
    if source == "catalog":
        from hextech.infrastructure.sources.catalog_versioned import refresh_catalog

        result = refresh_catalog(
            force=force,
            allow_remote=True,
            promote_current=False,
            pointer_output=pointer_output,
        )
    elif source == "hextech":
        from hextech.infrastructure.sources.hextech.service import main_scraper

        result = main_scraper(
            stop_event,
            force=force,
            promote_current=False,
            pointer_output=pointer_output,
        )
    elif source == "apex":
        from hextech.infrastructure.sources.apex.service import main as refresh_apex

        result = refresh_apex(
            dry_run=False,
            promote_current=False,
            pointer_output=pointer_output,
        )
    elif source == "mayhem":
        from hextech.infrastructure.sources.mayhem.service import run_mayhem_refresh

        result = run_mayhem_refresh(
            force=force,
            promote_current=False,
            pointer_output=pointer_output,
        )
    else:
        raise ValueError(f"未知来源：{source}")

    if stop_event.is_set():
        raise RuntimeError(f"{source} worker 已取消")
    source_result = dict(result) if isinstance(result, Mapping) else {"success": bool(result)}
    if not pointer_output.is_file():
        raise RuntimeError(
            f"{source} 未生成 candidate pointer："
            f"source_result={json.dumps(source_result, ensure_ascii=False, default=str)}"
        )
    pointer = _load_pointer(pointer_output, source)
    return {
        "state": "ready",
        "source": source,
        "elapsed_seconds": time.monotonic() - started,
        "pointer": pointer,
        "source_result": source_result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hextech isolated acquisition worker")
    parser.add_argument("--source", required=True, choices=("catalog", "hextech", "apex", "mayhem"))
    parser.add_argument("--pointer-output", type=Path, required=True)
    parser.add_argument("--result-output", type=Path, required=True)
    parser.add_argument("--cancel-file", type=Path, required=True)
    parser.add_argument("--catalog-pointer", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_worker(
            args.source,
            force=args.force,
            pointer_output=args.pointer_output,
            cancel_file=args.cancel_file,
            catalog_pointer=args.catalog_pointer,
        )
        exit_code = 0
    except Exception as exc:
        result = {
            "state": "failed",
            "source": args.source,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }
        exit_code = 2
    atomic_write_json(args.result_output, result, ensure_ascii=False, indent=2)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_worker"]
