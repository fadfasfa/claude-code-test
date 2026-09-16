"""打包前清空受控 Hextech；不读取命令行，不终止其他程序。"""

from __future__ import annotations

from pathlib import Path

import psutil

APP_NAME = "Hextech伴生终端.exe"


def allowed_executable(executable: Path, install_dir: Path, artifacts_dir: Path) -> bool:
    path = executable.resolve()
    if path.name.casefold() != APP_NAME.casefold():
        return False
    if path == install_dir.resolve() / APP_NAME:
        return True
    for lane in ("releases", "staging"):
        root = (artifacts_dir / lane).resolve()
        if path.parent.parent == root and path.parent.name.startswith("HextechCompanion-"):
            return True
    return False


def shutdown_for_package(install_dir: Path, artifacts_dir: Path, *, timeout: float = 12.0) -> tuple[int, ...]:
    """先验证所有身份再关闭；PID 复用由 psutil Process 身份校验保护。"""
    targets = []
    for process in psutil.process_iter(["name"]):
        name = str(process.info.get("name") or "").casefold()
        if name == "league of legends.exe":
            raise RuntimeError("real_game_active: 打包前不关闭游戏，也不启动原生 smoke")
        if name != APP_NAME.casefold():
            continue
        try:
            executable = Path(process.exe())
            if not allowed_executable(executable, install_dir, artifacts_dir):
                raise RuntimeError(f"Hextech 路径不在打包允许范围，未结束任何进程：pid={process.pid}")
            process.create_time()
            targets.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise RuntimeError(f"Hextech 身份无法核验，未结束任何进程：pid={process.pid}") from exc
    for process in targets:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=max(0.1, timeout))
    for process in alive:
        try:
            # 重新验证路径；terminate/kill 本身还会检查 PID 是否被复用。
            if not allowed_executable(Path(process.exe()), install_dir, artifacts_dir):
                raise RuntimeError(f"Hextech 关闭期间身份变化：pid={process.pid}")
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3.0)
    if alive:
        raise RuntimeError(f"Hextech 尚未退出，拒绝继续打包：pids={[p.pid for p in alive]}")
    # 再枚举一次，检测退出过程中重新启动的运行态。
    remaining = [p.pid for p in psutil.process_iter(["name"])
                 if str(p.info.get("name") or "").casefold() == APP_NAME.casefold()]
    if remaining:
        raise RuntimeError(f"Hextech 重新出现，拒绝启动 smoke：pids={remaining}")
    return tuple(p.pid for p in targets)
