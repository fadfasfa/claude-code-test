"""Web CSV watcher 与 ASGI 生命周期。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.interfaces.web.backend.runtime import *
from hextech.interfaces.web.backend.lcu_runtime import *
async def csv_watcher_loop() -> None:
    """监视 DataService generation，并在整代切换后广播数据刷新事件。"""

    previous_generation_id = ""
    while True:
        try:
            status = _snapshot_client.status()
            generation_id = str(status.get("generation_id") or "")
            if generation_id and previous_generation_id and generation_id != previous_generation_id:
                logger.info("DataService generation 已切换：generation_id=%s", generation_id)
                await manager.broadcast({"type": "data_updated", "generation_id": generation_id})
            previous_generation_id = generation_id or previous_generation_id
        except Exception as exc:
            logger.warning("DataService generation 监视器错误：%s", exc)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_app):
    """Web 生命周期钩子。

    只创建 LCU 与 generation 监视两条长生命周期任务；刷新由 DataService 唯一发起。
    """
    task1 = asyncio.create_task(lcu_polling_loop())
    task2 = asyncio.create_task(csv_watcher_loop())
    yield
    task1.cancel()
    task2.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass
    try:
        await task2
    except asyncio.CancelledError:
        pass
    shutdown_web_executors(wait=False)


def find_available_port(start_port: int = 8000, max_attempts: int = 50) -> int:
    import socket

    for port_offset in range(max_attempts):
        port = start_port + port_offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"未能在端口范围 {start_port}-{start_port + max_attempts - 1} 找到可用端口")


def maybe_open_browser(port: int) -> None:
    if os.getenv("HEXTECH_OPEN_BROWSER", "1").lower() in {"0", "false", "no"}:
        return
    open_managed_browser(f"http://127.0.0.1:{port}", replace_existing=True)

__all__ = [name for name in globals() if not name.startswith("__")]
