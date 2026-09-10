"""测试 overlay 视觉识别门控。

调用方: pytest; 关键依赖: tooling.setup.vision。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class OverlayRecognitionGateTests(unittest.TestCase):
    def test_strict_observed_name_binding_rejects_unresolved_ambiguous_and_unbound_files(self):
        from PIL import Image

        from hextech.infrastructure.vision.template_build import _attach_observed_name_exemplars
        from hextech.infrastructure.vision.template_models import TemplateEntry

        entries = [
            TemplateEntry(augment_id="1", name="同名强化", tier="", summary=""),
            TemplateEntry(augment_id="2", name="同名强化", tier="", summary=""),
        ]
        cases = (
            ("unknown__1.png", {}, "unresolved"),
            ("同名强化__1.png", {}, "ambiguous"),
            ("shared__1.png", [("shared", "1"), ("shared", "2")], "ambiguous"),
            ("external__1.png", {"external": "999"}, "unbound"),
        )
        for filename, aliases, expected in cases:
            with self.subTest(expected=expected), TemporaryDirectory() as temp_dir:
                exemplar_dir = Path(temp_dir) / "vision" / "name_exemplars"
                exemplar_dir.mkdir(parents=True)
                Image.new("RGB", (120, 32), "black").save(exemplar_dir / filename)

                with self.assertRaisesRegex(ValueError, rf"{expected}=.*{filename}"):
                    _attach_observed_name_exemplars(
                        entries,
                        Path(temp_dir),
                        extra_aliases=aliases,
                        strict=True,
                    )

    def test_body_shard_suffix_uses_two_rightmost_glyphs_and_quorum(self):
        from PIL import Image

        from hextech.infrastructure.vision.sidecar_fingerprints import (
            _body_shard_name_scores,
            _body_shard_scene_present,
        )

        root = Path(__file__).resolve().parent / "fixtures/diagnostics/overlay_vision_fixtures"
        shard = [Image.open(path).convert("RGB") for path in sorted((root / "body_shard_20260621").glob("name_*.png"))]
        ordinary = [Image.open(path).convert("RGB") for path in sorted((root / "hextech_20260621").glob("name_*.png"))]

        shard_scores = _body_shard_name_scores(shard)
        ordinary_scores = _body_shard_name_scores(ordinary)

        self.assertTrue(_body_shard_scene_present(shard_scores))
        self.assertFalse(_body_shard_scene_present(ordinary_scores))
        self.assertGreaterEqual(sum(score >= 0.80 for score in shard_scores), 2)
        self.assertLess(max(ordinary_scores), 0.80)

    def test_body_shard_suffix_rejects_single_glyph_and_animation_blob(self):
        from PIL import Image, ImageDraw

        from hextech.infrastructure.vision.sidecar_fingerprints import (
            _body_shard_name_scores,
            _body_shard_scene_present,
        )

        single = Image.new("RGB", (295, 48), "black")
        ImageDraw.Draw(single).rectangle((120, 5, 148, 35), fill="white")
        animation = Image.new("RGB", (295, 48), "white")

        scores = _body_shard_name_scores([single, animation, single])

        self.assertEqual(scores, (0.0, 0.0, 0.0))
        self.assertFalse(_body_shard_scene_present(scores))

    def test_known_icon_shortlist_confusions_cannot_authorize_ready(self):
        from hextech.infrastructure.vision.matcher import candidate_from_slot

        for expected_name, wrong_name in (
            ("黎明使者的坚决", "砸开那颗蛋"),
            ("哎哟，我的硬币！", "升级：收集者"),
        ):
            wrong_candidate = {
                "augment_id": wrong_name,
                "name": wrong_name,
                "confidence": 0.74,
            }
            slot = {
                "slot": 0,
                "channels": {
                    "text": {"margin": 0.01, "top_candidates": [wrong_candidate]},
                    "text_alt": {
                        "margin": 0.01,
                        "top_candidates": [{**wrong_candidate, "confidence": 0.69}],
                    },
                    "icon": {"margin": 0.04, "top_candidates": [wrong_candidate]},
                    "icon_shortlist": {"top_candidates": [wrong_candidate]},
                    "text_narrowed": {"margin": 0.04, "top_candidates": [wrong_candidate]},
                    "text_alt_narrowed": {"margin": 0.04, "top_candidates": [wrong_candidate]},
                },
            }

            with self.subTest(expected=expected_name, rejected=wrong_name):
                self.assertIsNone(candidate_from_slot(slot))

    def test_epoch21_clear_icon_and_one_text_channel_form_only_medium_observation(self):
        from hextech.infrastructure.vision.matcher import candidate_from_slot

        def candidate(augment_id: str, name: str, confidence: float) -> dict[str, object]:
            return {
                "augment_id": augment_id,
                "name": name,
                "recognition_key": name,
                "confidence": confidence,
            }

        slot = {
            "slot": 2,
            "channels": {
                "text": {
                    "margin": 0.0089,
                    "top_candidates": [candidate("1390", "超凡邪恶", 0.797174)],
                },
                "text_alt": {
                    "margin": 0.0084,
                    "top_candidates": [candidate("2032", "鲨鱼诱饵", 0.811479)],
                },
                "icon": {
                    "margin": 0.0778,
                    "top_candidates": [candidate("1390", "超凡邪恶", 0.756614)],
                },
            },
        }

        observed = candidate_from_slot(slot)

        self.assertIsNotNone(observed)
        self.assertEqual(observed.name, "超凡邪恶")
        self.assertEqual(observed.rule, "text_icon_temporal")
        self.assertEqual(observed.evidence_grade, "medium")
        self.assertEqual(observed.required_frames, 3)

        # 图标区分度不足或另一路文字不再处于近并列时都必须拒绝。
        slot["channels"]["icon"]["margin"] = 0.0499
        self.assertIsNone(candidate_from_slot(slot))
        slot["channels"]["icon"]["margin"] = 0.0778
        slot["channels"]["text_alt"]["margin"] = 0.0101
        self.assertIsNone(candidate_from_slot(slot))

    def test_real_hard_name_exemplars_resolve_to_expected_canonical_ids(self):
        from PIL import Image

        from hextech.infrastructure.vision import sidecar
        from hextech.infrastructure.vision.matcher import candidate_from_slot
        from hextech.modules.data.generation import DataSnapshotClient

        run_dir = Path(__file__).resolve().parents[1]
        template_index = sidecar.load_default_template_index(run_dir)
        matrices = sidecar._rank_matrices(template_index)
        self.assertGreaterEqual(matrices.observed_name_matrix.shape[0], 4)
        self.assertEqual(
            {entry.augment_id for entry in matrices.observed_name_templates},
            {"1020", "1045", "1332", "2089"},
        )
        snapshot = DataSnapshotClient(run_dir / "resources" / "seeds").open_view()
        cases = (
            (
                run_dir
                / "tests/fixtures/diagnostics/overlay_vision_fixtures"
                / "hextech_20260721_hard_names/dawnbringersresolve_holdout.png",
                "黎明使者的坚决",
                "1020",
            ),
            (
                run_dir
                / "tests/fixtures/diagnostics/overlay_vision_fixtures"
                / "hextech_20260721_hard_names/yowchmycoins_observed.png",
                "哎哟，我的硬币！",
                "2089",
            ),
            (
                run_dir
                / "tests/fixtures/diagnostics/overlay_vision_fixtures"
                / "hextech_20260821_hard_names/infernalconduit_holdout.png",
                "炼狱导管",
                "1045",
            ),
            (
                run_dir
                / "tests/fixtures/diagnostics/overlay_vision_fixtures"
                / "hextech_20260821_hard_names/ominouspact_holdout.png",
                "不祥契约",
                "1332",
            ),
        )

        for path, expected_name, expected_id in cases:
            with self.subTest(name=expected_name), Image.open(path) as crop:
                fingerprint = sidecar._normalized_fingerprint(sidecar._text_levels(crop.convert("RGB")))
                ranked = sidecar._rank_observed_name_fingerprint(fingerprint, template_index)
                self.assertGreaterEqual(float(ranked[0][1]), 0.92)
                self.assertGreaterEqual(sidecar._candidate_margin(ranked), 0.08)
                slot = {
                    "slot": 2,
                    "channels": {
                        "observed_name": {
                            "margin": sidecar._candidate_margin(ranked),
                            "top_candidates": sidecar._top_candidates(ranked),
                        }
                    },
                }
                candidate = candidate_from_slot(slot)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.name, expected_name)
                self.assertEqual(candidate.rule, "observed_name")
                self.assertEqual(snapshot.resolve_augment(candidate.name)["canonical_id"], expected_id)

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
            mock.patch.object(vision_eval, "_load_timeline_truth", return_value=[]),
            mock.patch.object(vision_eval.overlay_vision_sidecar, "load_default_template_index", return_value=[]),
            mock.patch.object(vision_eval.overlay_vision_sidecar, "_rank_matrices"),
            mock.patch.object(vision_eval, "_evaluate_name_roi_sample", return_value=roi_result),
        ):
            summary = vision_eval.evaluate_truth(Path("truth.json"), min_confidence=0.0)

        self.assertIsNone(summary["frame_slot_accuracy"])
        self.assertEqual(summary["false_ready_count"], 0)
        self.assertIsNone(summary["accuracy"])
        self.assertEqual(summary["name_roi_accuracy"], 1.0)

    def test_temporal_disagreement_fixture_recovers_without_false_failure(self):
        from tooling.diagnostics import vision_eval

        run_dir = Path(__file__).resolve().parents[1]
        sample = vision_eval._load_timeline_truth(
            run_dir / "tests/fixtures/diagnostics/overlay_matching_truth.v1.json"
        )[0]

        result = vision_eval._evaluate_timeline_sample(sample, run_dir=run_dir)

        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["failures"], [])
        # 首槽两路文字在第 1/3 帧形成 strong-dual；低置信冲突 icon 只作诊断。
        self.assertEqual(result["ready_at"], [2, 2, 2])

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
