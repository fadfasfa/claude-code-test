"""无 Desktop 的 Web 调试栈：DataService + Web，可选 Overlay。"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.modules.data.generation import DataSnapshotClient


def _read_web_port(port_file: Path) -> int:
    raw = port_file.read_text(encoding="utf-8").strip()
    port = int(raw)
    if not 1024 <= port <= 65535:
        raise ValueError(f"Web 端口越界：{port}")
    return port


def _probe_web(url: str, *, timeout: float = 15.0) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - 固定 loopback URL
        return int(response.status)


def _existing_web_url(port_file: Path, probe_web: Callable[[str], int]) -> str:
    if not port_file.is_file():
        return ""
    try:
        url = f"http://127.0.0.1:{_read_web_port(port_file)}"
        return url if probe_web(url) == 200 else ""
    except Exception:
        return ""


def _stop_web_process(process: Any | None, *, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except Exception:
        process.kill()
        process.wait(timeout=timeout)


def run_web_debug_stack(
    *,
    no_browser: bool = False,
    probe_only: bool = False,
    with_overlay: bool = False,
    readiness_timeout: float = 30.0,
    start_data_service: Callable[..., Any] | None = None,
    stop_data_service: Callable[[Any], None] | None = None,
    start_web: Callable[..., Any] | None = None,
    overlay_factory: Callable[[], Any] | None = None,
    probe_web: Callable[[str], int] | None = None,
    snapshot_status: Callable[[], dict[str, Any]] | None = None,
    reuse_existing_web: bool = True,
) -> int:
    """运行独立调试栈；默认绝不创建 Desktop 或 Overlay。"""

    if start_data_service is None or stop_data_service is None or start_web is None:
        from hextech.interfaces.desktop.runtime_processes import (
            start_data_service_process,
            stop_data_service_process,
        )
        from hextech.interfaces.desktop.runtime_services import start_web_server_process

        start_data_service = start_data_service or start_data_service_process
        stop_data_service = stop_data_service or stop_data_service_process
        start_web = start_web or start_web_server_process

    port_file = Path(build_runtime_state_path("web_server_port.txt"))
    data_handle = None
    web_process = None
    overlay = None
    previous_skip_refresh = os.environ.get("HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH")
    probe = probe_web or _probe_web
    data_owner = "started"
    web_owner = "started"
    try:
        if probe_only:
            os.environ["HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH"] = "1"
        try:
            data_handle = start_data_service(parent_pid=os.getpid(), timeout=readiness_timeout)
            data_status = data_handle.get_status()
        except RuntimeError as exc:
            if "code=3" not in str(exc):
                raise
            data_status = (snapshot_status or DataSnapshotClient().status)()
            data_owner = "existing"
        generation_id = str(
            data_status.get("generation_id")
            or (data_status.get("snapshot") or {}).get("generation_id")
            or ""
        )
        if not generation_id:
            raise RuntimeError("DataService 未提供有效 generation")

        url = _existing_web_url(port_file, probe) if reuse_existing_web else ""
        if url:
            web_owner = "existing"
        else:
            web_process = start_web(
                str(port_file),
                auto_open_browser=False,
                timeout=readiness_timeout,
            )
            port = _read_web_port(port_file)
            url = f"http://127.0.0.1:{port}"

        if with_overlay:
            if overlay_factory is None:
                from hextech.interfaces.overlay.lifecycle import GameOverlayController

                overlay_factory = GameOverlayController
            overlay = overlay_factory()
            overlay.start()

        print(
            json.dumps(
                {
                    "data_service": "ready",
                    "data_service_owner": data_owner,
                    "generation_id": generation_id,
                    "web_url": url,
                    "web_owner": web_owner,
                    "overlay": "enabled" if with_overlay else "disabled",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if probe_only:
            status = probe(url)
            if status != 200:
                raise RuntimeError(f"Web 首页状态码异常：{status}")
            print("Web debug stack probe passed: DataService ready, HTTP 200", flush=True)
            return 0

        if not no_browser:
            webbrowser.open(url)
        if web_process is None:
            while probe(url) == 200:
                time.sleep(1.0)
        else:
            while web_process.poll() is None:
                time.sleep(0.5)
            if int(web_process.returncode or 0) != 0:
                raise RuntimeError(f"hextech-web 退出码：{web_process.returncode}")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if overlay is not None:
            overlay.stop()
        _stop_web_process(web_process)
        if data_handle is not None and stop_data_service is not None:
            stop_data_service(data_handle)
        if previous_skip_refresh is None:
            os.environ.pop("HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH", None)
        else:
            os.environ["HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH"] = previous_skip_refresh


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 DataService + Web 调试栈，不启动 Desktop。")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器。")
    parser.add_argument("--probe-only", action="store_true", help="验证 DataService 和 Web 后立即回收。")
    parser.add_argument("--with-overlay", action="store_true", help="额外启动 Overlay host 和 Vision sidecar。")
    parser.add_argument("--readiness-timeout", type=float, default=30.0, help="单个服务 readiness 超时秒数。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_web_debug_stack(
        no_browser=args.no_browser,
        probe_only=args.probe_only,
        with_overlay=args.with_overlay,
        readiness_timeout=max(1.0, min(float(args.readiness_timeout), 60.0)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
