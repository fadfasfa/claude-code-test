"""显式单次游戏客户区采集入口；仅写本工作树 .artifacts 下新建的隔离目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import time
import uuid

RUN_DIR = Path(__file__).resolve().parents[2]
if str(RUN_DIR / "src") not in sys.path:
    sys.path.insert(0, str(RUN_DIR / "src"))

OUTPUT_PARTS = (".artifacts", "overlay-capture-once")


def new_output_directory(run_dir: Path) -> Path:
    """不接受任意输出路径；拒绝固定根中的 junction/symlink，mkdir 不覆盖旧证据。"""
    current = run_dir.resolve(strict=True)
    for part in OUTPUT_PARTS:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        metadata = current.lstat()
        if (current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not current.is_dir()):
            raise ValueError("isolated_output_reparse_or_not_directory")
    destination = current / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex)
    destination.mkdir(exist_ok=False)
    return destination


def source_identity() -> dict[str, str]:
    # 精确绑定采集器与实际读取的几何/窗口代码，不冒充正式 runtime 的 Build。
    relative_paths = [
        "tooling/diagnostics/overlay_capture_once.py", "src/hextech/infrastructure/vision/capture_once.py",
        "src/hextech/modules/vision/window.py", "src/hextech/modules/vision/game_window_mode.py",
        "src/hextech/modules/vision/layout.py", "src/hextech/interfaces/overlay/display_geometry.py",
        "src/hextech/interfaces/overlay/display_contract.py",
        "src/hextech/modules/session/build_identity.py",
    ]
    return {name: hashlib.sha256((RUN_DIR / name).read_bytes()).hexdigest() for name in relative_paths}


def write_evidence(destination: Path, png: bytes, metadata: dict, deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("capture_budget_exceeded")
    with (destination / "client.png").open("xb") as stream:
        stream.write(png)
    if time.monotonic() >= deadline:
        raise TimeoutError("capture_budget_exceeded")
    # manifest 最后完成才是有效证据；任何截断/孤立 PNG 保留但不能用于验收。
    with (destination / "capture.json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _worker(destination: str, deadline: float, sender) -> None:
    try:
        from hextech.infrastructure.vision.capture_once import CaptureRejected, collect_once
        from hextech.interfaces.overlay.display_geometry import display_geometry_metadata
        from hextech.modules.vision.window import configure_process_dpi_awareness

        if configure_process_dpi_awareness() != "per_monitor_v2":
            raise CaptureRejected("per_monitor_v2_required")
        source_before = source_identity()
        png, metadata = collect_once(enabled=True, deadline=deadline)
        metadata["layout"] = display_geometry_metadata(tuple(metadata["image"]["size"]))
        if source_before != source_identity():
            raise CaptureRejected("capture_source_changed")
        metadata["collector_source_sha256"] = source_before
        write_evidence(Path(destination), png, metadata, deadline)
        sender.send({"status": "captured", "output": destination})
    except Exception as exc:
        from hextech.infrastructure.vision.capture_once import CaptureRejected

        reason = str(exc) if isinstance(exc, CaptureRejected) else type(exc).__name__
        sender.send({"status": "rejected", "reason": reason, "output": destination})
    finally:
        sender.close()


def run_bounded(destination: Path, budget: float, *, context=None) -> dict:
    """只拉起本采集 worker，不启动任何生产角色；超时只终止自己创建的子进程。"""
    ctx = context or multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    deadline = time.monotonic() + budget
    process = ctx.Process(target=_worker, args=(str(destination), deadline, sender), daemon=True)
    try:
        process.start()
        sender.close()
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
            return {"status": "rejected", "reason": "capture_budget_exceeded",
                    "worker_stopped": not process.is_alive(), "output": str(destination)}
        if receiver.poll():
            try:
                return receiver.recv()
            except EOFError:
                pass
        return {"status": "rejected", "reason": "capture_worker_failed", "output": str(destination)}
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None and process.is_alive():
            process.terminate()
            process.join(1.0)


def main(argv: list[str] | None = None) -> int:
    # Windows 管道默认代码页不一定是 UTF-8；一键入口的中文提示必须可读。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", help="显式授权一次游戏客户区 PNG；无此参数零采集零写入")
    parser.add_argument("--delay-seconds", type=float, default=3.0, help="一次倒计时，0–10 秒，供手动切回游戏")
    parser.add_argument("--budget-seconds", type=float, default=5.0, help="子进程含启动/分析/写入硬预算，1–10 秒")
    args = parser.parse_args(argv)
    if not args.capture:
        print(json.dumps({"status": "disabled", "reason": "explicit_capture_required"}))
        return 2
    if (not math.isfinite(args.delay_seconds) or not 0 <= args.delay_seconds <= 10
            or not math.isfinite(args.budget_seconds) or not 1 <= args.budget_seconds <= 10):
        parser.error("delay must be 0–10 seconds; budget must be 1–10 seconds")
    if os.name != "nt":
        parser.error("Windows only")
    print(f"单次采集已开启：{args.delay_seconds:g} 秒后检查前台游戏；最多一帧，无重试。", flush=True)
    time.sleep(args.delay_seconds)
    try:
        destination = new_output_directory(RUN_DIR)
        result = run_bounded(destination, args.budget_seconds)
    except (OSError, ValueError) as exc:
        result = {"status": "rejected", "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "captured" else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
