"""测试原子写入工具的并发和落盘边界。

调用方: pytest; 关键依赖: hextech.modules.data.ports.atomic。
"""
from __future__ import annotations

import gc
import json
from unittest.mock import patch


def test_atomic_write_fsyncs_temp_file_before_replace(tmp_path):
    from hextech.modules.data.ports import atomic as atomic_io

    calls: list[int] = []

    with patch.object(atomic_io.os, "fsync", side_effect=lambda fd: calls.append(fd)):
        atomic_io.atomic_write_text(tmp_path / "state.txt", "ready")

    assert calls


def test_atomic_write_lock_entry_is_released_after_write(tmp_path):
    from hextech.modules.data.ports import atomic as atomic_io

    target = tmp_path / "state.json"
    key = atomic_io._target_key(str(target))

    atomic_io.atomic_write_json(target, {"ok": True})
    gc.collect()

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert key not in atomic_io._WRITE_LOCKS
