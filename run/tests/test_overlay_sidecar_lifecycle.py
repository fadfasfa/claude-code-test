"""测试 overlay sidecar 生命周期。

调用方: pytest; 关键依赖: hextech.overlay.lifecycle。
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


class OverlaySidecarLifecycleTests(unittest.TestCase):
    def test_lifecycle_waits_for_sidecar_ready_and_sets_exit_signal(self):
        from hextech.overlay import lifecycle

        source = inspect.getsource(lifecycle.start_sidecar_process)

        self.assertEqual(lifecycle.OVERLAY_SIDECAR_READY_FILE_ENV, "HEXTECH_OVERLAY_SIDECAR_READY_FILE")
        self.assertIn("OVERLAY_SIDECAR_READY_FILE_ENV", source)
        self.assertIn("_wait_for_sidecar_ready", source)
        self.assertIn("_hextech_overlay_exit_file", source)

    def test_sidecar_writes_ready_and_checks_exit_signal(self):
        from hextech.overlay.vision import sidecar

        run_loop_source = inspect.getsource(sidecar.run_loop)

        self.assertIn("_write_sidecar_ready_from_env", run_loop_source)
        self.assertIn("_sidecar_exit_requested", run_loop_source)

    def test_context_poller_failure_degrades_without_blocking_overlay(self):
        from hextech.overlay.lifecycle import GameOverlayController

        class FakeProcess:
            pid = 123

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        controller = GameOverlayController(
            start_host_func=lambda: FakeProcess(),
            start_sidecar_func=lambda: FakeProcess(),
            start_context_poller_func=lambda: (_ for _ in ()).throw(RuntimeError("LCU offline")),
            prepare_data_func=lambda: None,
            write_inactive_func=lambda: None,
        )

        controller.start()
        snapshot = controller.snapshot()

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["context_poller_status"], "degraded")
        self.assertIn("LCU offline", snapshot["context_poller_error"])

    def test_context_poller_starts_before_sidecar_cold_start(self):
        from hextech.overlay.lifecycle import GameOverlayController

        calls: list[str] = []

        class FakeProcess:
            pid = 123

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        controller = GameOverlayController(
            start_host_func=lambda: (calls.append("host"), FakeProcess())[1],
            start_sidecar_func=lambda: (calls.append("sidecar"), FakeProcess())[1],
            start_context_poller_func=lambda: (calls.append("context"), object())[1],
            prepare_data_func=lambda: calls.append("prepare"),
            write_inactive_func=lambda: calls.append("inactive"),
        )

        controller.start()

        self.assertEqual(calls[:4], ["prepare", "inactive", "context", "sidecar"])

    def test_template_runtime_signature_ignores_hint_cache_runtime_metadata(self):
        from hextech.overlay.vision import sidecar

        base_cache = {
            "schema_version": 1,
            "generated_at": 100.0,
            "source": {"tag": "desktop-game-overlay", "private_policy_stats_enabled": True},
            "hints": {
                "augment-a": {
                    "augment_id": "augment-a",
                    "name": "测试海克斯",
                    "icon": "icons/a.png",
                    "summary": "说明",
                }
            },
            "name_index": {"测试海克斯": "augment-a"},
        }
        changed_metadata = {
            **base_cache,
            "generated_at": 200.0,
            "source": {"tag": "mayhem-refresh", "private_policy_stats_enabled": True},
        }
        changed_identity = {
            **base_cache,
            "hints": {
                "augment-a": {
                    "augment_id": "augment-a",
                    "name": "另一个海克斯",
                    "icon": "icons/a.png",
                    "summary": "说明",
                }
            },
        }
        changed_name_index = {
            **base_cache,
            "name_index": {"测试海克斯": "augment-b"},
        }

        self.assertEqual(sidecar.template_runtime_hint_signature(base_cache), sidecar.template_runtime_hint_signature(changed_metadata))
        self.assertNotEqual(sidecar.template_runtime_hint_signature(base_cache), sidecar.template_runtime_hint_signature(changed_identity))
        self.assertNotEqual(sidecar.template_runtime_hint_signature(base_cache), sidecar.template_runtime_hint_signature(changed_name_index))

    def test_template_runtime_cache_v2_manifest_and_float16_roundtrip(self):
        from hextech.overlay.vision import sidecar

        template_index = [
            sidecar.TemplateEntry(
                augment_id="augment-a",
                name="测试海克斯 A",
                tier="silver",
                summary="说明 A",
                fingerprint=(0.5, -0.5, 0.25, -0.25),
                icon_fingerprints=((0.5, -0.5, 0.25, -0.25),),
                icon_digest="digest-a",
                priority=1,
                name_fingerprint=(0.1, 0.2, -0.1, -0.2),
                name_fingerprint_alt=(0.2, 0.1, -0.2, -0.1),
                source_icon_filenames=("a.png",),
            ),
            sidecar.TemplateEntry(
                augment_id="augment-b",
                name="测试海克斯 B",
                tier="gold",
                summary="说明 B",
                fingerprint=(-0.5, 0.5, -0.25, 0.25),
                icon_fingerprints=((-0.5, 0.5, -0.25, 0.25),),
                icon_digest="digest-b",
                priority=2,
                name_fingerprint=(-0.1, -0.2, 0.1, 0.2),
                name_fingerprint_alt=(-0.2, -0.1, 0.2, 0.1),
                source_icon_filenames=("b.png",),
            ),
        ]
        with patch.object(sidecar, "_cleaned_name_fingerprint", return_value=None):
            matrices = sidecar.rank_template_matrices(template_index)
        signature = {"schema_version": sidecar.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION, "resource": "fixture"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "template_runtime_cache.v2.npz"
            write_stats = sidecar._write_template_runtime_cache(
                cache_file,
                resource_signature=signature,
                hint_signature="hint-fixture",
                template_index=template_index,
                matrices=matrices,
            )

            self.assertEqual(write_stats["schema_version"], 2)
            self.assertEqual(write_stats["matrix_dtype"], "float16")
            with np.load(cache_file, allow_pickle=False) as payload:
                manifest = json.loads(np.asarray(payload["manifest_json"], dtype=np.uint8).tobytes().decode("utf-8"))
                manifest_text = json.dumps(manifest, ensure_ascii=False)
                self.assertEqual(manifest["schema_version"], 2)
                self.assertEqual(payload["icon_matrix"].dtype, np.float16)
                self.assertNotIn("fingerprint", manifest_text)
                self.assertNotIn("name_fingerprint", manifest_text)

            runtime = sidecar._read_template_runtime_cache(
                cache_file,
                resource_signature=signature,
                hint_signature="hint-fixture",
            )

        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertTrue(runtime.stats["cache_hit"])
        self.assertEqual(runtime.stats["schema_version"], 2)
        self.assertEqual(runtime.stats["matrix_dtype"], "float16")
        self.assertEqual(runtime.matrices.icon_matrix.dtype, np.float32)
        self.assertEqual(runtime.template_index[0].fingerprint, ())
        np.testing.assert_allclose(runtime.matrices.icon_matrix, matrices.icon_matrix, atol=1e-3)
        sidecar._RANK_MATRIX_CACHE.pop(id(runtime.template_index), None)
        restored_matrices = sidecar.rank_template_matrices(runtime.template_index)
        self.assertIs(restored_matrices, runtime.matrices)
        np.testing.assert_allclose(restored_matrices.name_matrix, matrices.name_matrix, atol=1e-3)

    def test_template_runtime_cache_v2_ready_cleans_default_v1_cache(self):
        from hextech.overlay.vision import sidecar

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            v1_cache = root / "template_runtime_cache.v1.npz"
            v2_cache = root / "template_runtime_cache.v2.npz"
            v1_cache.write_bytes(b"legacy-cache")
            v2_cache.write_bytes(b"v2-cache")
            with (
                patch.object(sidecar, "TEMPLATE_RUNTIME_CACHE_FILE", v2_cache),
                patch.object(sidecar, "TEMPLATE_RUNTIME_CACHE_V1_FILE", v1_cache),
            ):
                self.assertTrue(sidecar._cleanup_legacy_template_runtime_cache(v2_cache))
                self.assertFalse(v1_cache.exists())

            custom_cache = root / "custom.v2.npz"
            v1_cache.write_bytes(b"legacy-cache")
            custom_cache.write_bytes(b"custom-cache")
            with (
                patch.object(sidecar, "TEMPLATE_RUNTIME_CACHE_FILE", v2_cache),
                patch.object(sidecar, "TEMPLATE_RUNTIME_CACHE_V1_FILE", v1_cache),
            ):
                self.assertFalse(sidecar._cleanup_legacy_template_runtime_cache(custom_cache))
                self.assertTrue(v1_cache.exists())


if __name__ == "__main__":
    unittest.main()
