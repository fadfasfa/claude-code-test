"""测试 桌面单实例锁。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.single_instance。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class DesktopSingleInstanceTests(unittest.TestCase):
    def test_live_owner_rejects_second_instance(self):
        from hextech.interfaces.desktop.single_instance import DesktopInstanceAlreadyRunning, DesktopInstanceOwner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "locks" / "desktop_ui.lock"
            owner = root / "state" / "desktop_ui_owner.v1.json"
            first = DesktopInstanceOwner(lock, owner)
            first.acquire()
            try:
                with self.assertRaises(DesktopInstanceAlreadyRunning) as raised:
                    DesktopInstanceOwner(lock, owner).acquire()
                self.assertEqual(int(raised.exception.owner["pid"]), os.getpid())
                self.assertTrue(raised.exception.activation_sent)
                request = first.consume_activation_request()
                self.assertIsNotNone(request)
                self.assertEqual(request["target_owner_id"], first.owner_id)
            finally:
                first.release()

    def test_stale_owner_is_replaced(self):
        from hextech.interfaces.desktop.single_instance import DesktopInstanceOwner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "desktop_ui.lock"
            owner = root / "desktop_ui_owner.v1.json"
            lock.write_text("stale", encoding="utf-8")
            owner.write_text(json.dumps({"pid": 99999999, "cwd": "old"}), encoding="utf-8")

            instance = DesktopInstanceOwner(lock, owner)
            instance.acquire()
            try:
                payload = json.loads(owner.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
                self.assertNotEqual(payload.get("cwd"), "old")
            finally:
                instance.release()

    def test_different_lock_paths_do_not_conflict(self):
        from hextech.interfaces.desktop.single_instance import DesktopInstanceOwner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = DesktopInstanceOwner(root / "a" / "desktop_ui.lock", root / "a" / "owner.json")
            second = DesktopInstanceOwner(root / "b" / "desktop_ui.lock", root / "b" / "owner.json")
            first.acquire()
            try:
                second.acquire()
                second.release()
            finally:
                first.release()

    def test_activation_for_different_owner_is_ignored(self):
        from hextech.interfaces.desktop.single_instance import DesktopInstanceOwner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = DesktopInstanceOwner(root / "desktop.lock", root / "owner.json")
            instance.acquire()
            try:
                Path(instance.activation_path).write_text(
                    json.dumps(
                        {
                            "request_id": "wrong-owner",
                            "target_owner_id": "another-owner",
                            "requester_pid": 123,
                            "requested_at": __import__("time").time(),
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertIsNone(instance.consume_activation_request())
            finally:
                instance.release()


if __name__ == "__main__":
    unittest.main()
