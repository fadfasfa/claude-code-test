"""首代发布和订阅恢复只用假快照、假时钟与假浏览器测试。"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("generations,expected", [
    (["", "A", "A"], ["A"]),
    (["A", "A", "B", "B"], ["B"]),
    (["", "", ""], []),
    (["A", "", "A"], ["A"]),
])
def test_generation_watcher_notifies_availability_transitions(monkeypatch, generations, expected):
    from hextech.interfaces.web.backend import app_lifecycle

    messages = []
    states = iter(generations)
    sleeps = 0

    async def broadcast(message):
        messages.append(message)

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == len(generations):
            raise asyncio.CancelledError

    monkeypatch.setattr(app_lifecycle, "_snapshot_client", SimpleNamespace(
        status=lambda: {"generation_id": next(states)}))
    monkeypatch.setattr(app_lifecycle, "manager", SimpleNamespace(broadcast=broadcast))
    monkeypatch.setattr(app_lifecycle, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_lifecycle.csv_watcher_loop())
    assert [message["generation_id"] for message in messages] == expected


def test_index_generation_sync_with_fake_browser():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the fake-browser ESM regression")
    script = Path(__file__).parent / "frontend" / "test_index_generation_sync.mjs"
    result = subprocess.run([node, "--experimental-vm-modules", str(script)],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
