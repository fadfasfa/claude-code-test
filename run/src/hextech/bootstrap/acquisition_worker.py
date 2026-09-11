"""单一来源的隔离抓取 worker 入口。

本进程只写 immutable run、报告和 candidate pointer，不得直接切正式 current。
DataService coordinator 在所有来源都通过后，才通过 promotion journal 提升 cohort。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json


class SourceRefreshFailed(RuntimeError):
    """来源未产生新 candidate；payload 可安全写入跨进程结果。"""

    def __init__(self, source: str, payload: Mapping[str, Any]) -> None:
        self.source = source
        self.payload = dict(payload)
        reason = str(self.payload.get("reason_code") or "candidate_pointer_missing")
        super().__init__(f"{source} 未生成 candidate pointer：reason={reason}")


def _hextech_worker_result(result: Mapping[str, Any] | bool, pointer_output: Path) -> dict[str, Any]:
    if pointer_output.is_file():
        return {"state": "ready", "success": bool(result)}

    from hextech.infrastructure.sources.hextech.refresh_support import load_scraper_status

    status = load_scraper_status()
    last_result = str(status.get("last_result") or "failed")
    return {
        "state": last_result if last_result in {"fallback", "failed"} else "failed",
        "success": False,
        "reason_code": str(status.get("reason") or "candidate_pointer_missing"),
        "failure_stage": str(status.get("failure_stage") or ""),
        "fallback_used": bool(status.get("fallback_used")),
        "last_good_available": bool(status.get("active_csv")),
        "diagnostics": dict(status.get("failure_diagnostics") or {}),
    }


def _missing_pointer_payload(source_result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reason_code": str(
            source_result.get("reason_code")
            or source_result.get("reason")
            or "candidate_pointer_missing"
        ),
        "failure_stage": str(source_result.get("failure_stage") or ""),
        "fallback_used": bool(source_result.get("fallback_used")),
        "last_good_available": bool(source_result.get("last_good_available")),
        "diagnostics": dict(source_result.get("diagnostics") or {}),
    }


def _native_path_too_long(exc: Exception) -> bool:
    text = str(exc or "").casefold()
    return bool(
        getattr(exc, "winerror", None) == 206
        or "文件名或扩展名太长" in text
        or "filename or extension is too long" in text
        or "file name or extension is too long" in text
    )


def _unexpected_failure_payload(
    exc: Exception,
    *,
    failure_stage: str = "worker_execution",
) -> dict[str, Any]:
    """所有 worker 异常保持同一有限失败契约，供 coordinator 稳定分类。"""

    if _native_path_too_long(exc):
        return {
            "reason_code": "native_runtime_path_too_long",
            "failure_stage": "worker_import",
            "fallback_used": False,
            "last_good_available": False,
            "diagnostics": {
                "error_type": exc.__class__.__name__,
                "native_path_limit": 259,
            },
        }
    return {
        "reason_code": "worker_exception",
        "failure_stage": failure_stage,
        "fallback_used": False,
        "last_good_available": False,
        "diagnostics": {"error_type": exc.__class__.__name__},
    }


def run_import_self_check() -> dict[str, Any]:
    """无网络验证冻结 worker 的原生依赖与两条 Core source import。"""

    checks: dict[str, str] = {}
    for module_name in (
        "curl_cffi._wrapper",
        "hextech.infrastructure.sources.aramkit.service",
        "hextech.infrastructure.sources.blitz.service",
    ):
        importlib.import_module(module_name)
        checks[module_name] = "ready"
    from hextech.modules.session.build_identity import current_build_id

    return {
        "schema_version": 1,
        "state": "ready",
        "build_id": current_build_id(),
        "checks": checks,
    }


def _reuse_current_when_not_stale(source: str, result: Mapping[str, Any], pointer_output: Path) -> bool:
    """来源明确无需刷新时，把已验证 current 作为本轮 contribution 返回。"""

    reason = str(result.get("reason") or result.get("reason_code") or "")
    if reason != "not_stale":
        return False
    from hextech.modules.data.ports.paths import get_var_dir

    root = get_var_dir()
    current = (
        root / "catalog" / "current.v2.json"
        if source == "catalog"
        else root / "sources" / source / "current.v2.json"
    )
    if not current.is_file():
        return False
    pointer = _load_pointer(current, source)
    atomic_write_json(pointer_output, pointer, ensure_ascii=False, indent=2)
    return True


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
    catalog_compatibility_pointer: Path | None = None,
    coverage_policy: str = "catalog_adoption",
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
    elif source == "aramkit":
        from hextech.infrastructure.sources.aramkit.service import CatalogBinding, refresh_aramkit

        result = refresh_aramkit(
            force=force,
            promote_current=False,
            pointer_output=pointer_output,
            stop_event=stop_event,
            catalog_binding=CatalogBinding.active(
                compatibility_pointer=catalog_compatibility_pointer
            ),
        )
    elif source == "blitz":
        from hextech.infrastructure.sources.blitz.service import (
            CatalogBinding as BlitzCatalogBinding,
            refresh_blitz,
        )

        result = refresh_blitz(
            force=force,
            promote_current=False,
            pointer_output=pointer_output,
            stop_event=stop_event,
            catalog_binding=BlitzCatalogBinding.active(
                compatibility_pointer=catalog_compatibility_pointer
            ),
            coverage_policy=coverage_policy,
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
    source_result = (
        dict(result) if isinstance(result, Mapping) else {"success": bool(result)}
    )
    if not pointer_output.is_file() and not _reuse_current_when_not_stale(source, source_result, pointer_output):
        raise SourceRefreshFailed(source, _missing_pointer_payload(source_result))
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
    parser.add_argument("--source", choices=("catalog", "aramkit", "blitz", "apex", "mayhem"))
    parser.add_argument("--pointer-output", type=Path)
    parser.add_argument("--result-output", type=Path, required=True)
    parser.add_argument("--cancel-file", type=Path)
    parser.add_argument("--catalog-pointer", type=Path)
    parser.add_argument("--catalog-compatibility-pointer", type=Path)
    parser.add_argument(
        "--coverage-policy",
        choices=("catalog_adoption", "active_partial"),
        default="catalog_adoption",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_check:
        missing = [
            name
            for name, value in (
                ("--source", args.source),
                ("--pointer-output", args.pointer_output),
                ("--cancel-file", args.cancel_file),
            )
            if value is None
        ]
        if missing:
            parser.error("普通 worker 缺少必要参数：" + ", ".join(missing))
    try:
        if args.self_check:
            result = run_import_self_check()
        else:
            assert args.source is not None
            assert args.pointer_output is not None
            assert args.cancel_file is not None
            result = run_worker(
                args.source,
                force=args.force,
                pointer_output=args.pointer_output,
                cancel_file=args.cancel_file,
                catalog_pointer=args.catalog_pointer,
                catalog_compatibility_pointer=args.catalog_compatibility_pointer,
                coverage_policy=args.coverage_policy,
            )
        exit_code = 0
    except SourceRefreshFailed as exc:
        result = {
            "state": "failed",
            "source": args.source,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            **exc.payload,
        }
        exit_code = 2
    except Exception as exc:
        result = {
            "state": "failed",
            "source": str(args.source or "self_check"),
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            **_unexpected_failure_payload(
                exc,
                failure_stage="worker_import" if args.self_check else "worker_execution",
            ),
        }
        exit_code = 2
    atomic_write_json(args.result_output, result, ensure_ascii=False, indent=2)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_import_self_check", "run_worker"]
