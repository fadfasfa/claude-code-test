"""测试 overlay 视觉识别门控。

调用方: pytest; 关键依赖: tools.refresh_overlay_recognition。
"""
from __future__ import annotations

import unittest
from unittest import mock


class OverlayRecognitionGateTests(unittest.TestCase):
    def test_zero_full_frame_samples_block_validation(self):
        from tools import refresh_overlay_recognition

        with (
            mock.patch.object(refresh_overlay_recognition, "validate_official_catalog", return_value={
                "missing_field_count": 0,
                "duplicate_stable_id_count": 0,
                "missing_icon_count": 0,
                "invalid_icon_count": 0,
            }),
            mock.patch.object(refresh_overlay_recognition.overlay_vision_sidecar, "audit_default_template_index", return_value={
                "missing_identity_count": 0,
                "missing_variant_count": 0,
            }),
            mock.patch.object(refresh_overlay_recognition, "run_synthetic_recognition", return_value={
                "synthetic_failure_count": 0,
            }),
            mock.patch.object(refresh_overlay_recognition, "run_fixture_regression", return_value={
                "full_frame_sample_count": 0,
                "missing_count": 0,
                "invalid_path_count": 0,
                "fixture_missing_count": 0,
                "fixture_failure_count": 0,
            }),
        ):
            summary = refresh_overlay_recognition.validate_snapshot(refresh_overlay_recognition.RUN_DIR)

        self.assertFalse(summary["passed"])
        self.assertEqual(
            summary["blockers"]["full_frame_sample_deficit"],
            refresh_overlay_recognition.MIN_FULL_FRAME_SAMPLE_COUNT,
        )


if __name__ == "__main__":
    unittest.main()
