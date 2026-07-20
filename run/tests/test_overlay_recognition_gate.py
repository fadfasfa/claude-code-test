"""测试 overlay 视觉识别门控。

调用方: pytest; 关键依赖: tooling.setup.vision。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock


class OverlayRecognitionGateTests(unittest.TestCase):
    def test_name_roi_accuracy_does_not_masquerade_as_frame_accuracy(self):
        from tooling.diagnostics import vision_eval

        roi_result = {
            "id": "roi-only",
            "status": "evaluated",
            "checks": [
                {"kind": "name_top1", "expected": "尤里卡", "observed": "尤里卡", "matched": True}
            ],
        }
        with (
            mock.patch.object(vision_eval, "_load_truth", return_value=[]),
            mock.patch.object(vision_eval, "_load_name_roi_truth", return_value=[{"id": "roi-only"}]),
            mock.patch.object(vision_eval.overlay_vision_sidecar, "load_default_template_index", return_value=[]),
            mock.patch.object(vision_eval.overlay_vision_sidecar, "_rank_matrices"),
            mock.patch.object(vision_eval, "_evaluate_name_roi_sample", return_value=roi_result),
        ):
            summary = vision_eval.evaluate_truth(Path("truth.json"), min_confidence=0.0)

        self.assertIsNone(summary["frame_slot_accuracy"])
        self.assertEqual(summary["false_ready_count"], 0)
        self.assertIsNone(summary["accuracy"])
        self.assertEqual(summary["name_roi_accuracy"], 1.0)

    def test_zero_full_frame_samples_block_validation(self):
        from tooling.setup import vision as refresh_overlay_recognition

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
