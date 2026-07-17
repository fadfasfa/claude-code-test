"""测试 WebSocket 连接池的并发上限与广播隔离。"""
from __future__ import annotations

import asyncio

from hextech.interfaces.web.backend.runtime import ConnectionManager


class _ImmediateWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed = None
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


class _BlockingAcceptWebSocket(_ImmediateWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.accept_started = asyncio.Event()
        self.release_accept = asyncio.Event()

    async def accept(self) -> None:
        self.accept_started.set()
        await self.release_accept.wait()
        self.accepted = True


class _SlowWebSocket(_ImmediateWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send_json(self, message: dict) -> None:
        self.send_started.set()
        await self.release_send.wait()
        await super().send_json(message)


class _FailingWebSocket(_ImmediateWebSocket):
    async def send_json(self, message: dict) -> None:
        raise RuntimeError("send failed")


def test_connect_reserves_capacity_before_accept_finishes():
    async def scenario() -> None:
        manager = ConnectionManager()
        manager.max_connections = 1
        first = _BlockingAcceptWebSocket()
        second = _ImmediateWebSocket()

        first_task = asyncio.create_task(manager.connect(first))
        await first.accept_started.wait()
        await manager.connect(second)
        first.release_accept.set()
        await first_task

        assert manager.active == [first]
        assert second.accepted is False
        assert second.closed == (1013, "too_many_connections")

    asyncio.run(scenario())


def test_cancelled_accept_releases_reserved_capacity():
    async def scenario() -> None:
        manager = ConnectionManager()
        manager.max_connections = 1
        cancelled = _BlockingAcceptWebSocket()
        replacement = _ImmediateWebSocket()

        cancelled_task = asyncio.create_task(manager.connect(cancelled))
        await cancelled.accept_started.wait()
        cancelled_task.cancel()
        try:
            await cancelled_task
        except asyncio.CancelledError:
            pass

        await manager.connect(replacement)

        assert manager.active == [replacement]
        assert replacement.accepted is True
        assert replacement.closed is None

    asyncio.run(scenario())


def test_cancelled_registration_releases_reserved_capacity():
    async def scenario() -> None:
        manager = ConnectionManager()
        manager.max_connections = 1
        cancelled = _BlockingAcceptWebSocket()
        replacement = _ImmediateWebSocket()

        cancelled_task = asyncio.create_task(manager.connect(cancelled))
        await cancelled.accept_started.wait()
        await manager._lock.acquire()
        cancelled.release_accept.set()
        await _wait_for_accept(cancelled)
        cancelled_task.cancel()
        manager._lock.release()
        try:
            await cancelled_task
        except asyncio.CancelledError:
            pass

        await manager.connect(replacement)

        assert manager.active == [replacement]
        assert replacement.accepted is True
        assert replacement.closed is None

    asyncio.run(scenario())


def test_broadcast_sends_to_snapshot_concurrently():
    async def scenario() -> None:
        manager = ConnectionManager()
        slow = _SlowWebSocket()
        fast = _ImmediateWebSocket()
        manager.active = [slow, fast]

        broadcast_task = asyncio.create_task(manager.broadcast({"type": "update"}))
        await asyncio.wait_for(slow.send_started.wait(), timeout=0.2)
        try:
            await asyncio.wait_for(_wait_for_message(fast), timeout=0.2)
        finally:
            slow.release_send.set()
            await broadcast_task

        assert fast.messages == [{"type": "update"}]

    asyncio.run(scenario())


def test_broadcast_times_out_and_removes_slow_or_failing_connections():
    async def scenario() -> None:
        manager = ConnectionManager()
        slow = _SlowWebSocket()
        failing = _FailingWebSocket()
        fast = _ImmediateWebSocket()
        manager.active = [slow, failing, fast]

        await asyncio.wait_for(manager.broadcast({"type": "update"}), timeout=1.4)

        assert manager.active == [fast]
        assert fast.messages == [{"type": "update"}]

    asyncio.run(scenario())


async def _wait_for_message(ws: _ImmediateWebSocket) -> None:
    while not ws.messages:
        await asyncio.sleep(0)


async def _wait_for_accept(ws: _ImmediateWebSocket) -> None:
    while not ws.accepted:
        await asyncio.sleep(0)
