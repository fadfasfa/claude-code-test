"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Path,
    TemporaryDirectory,
    importlib,
    io,
    json,
    patch,
    sys,
)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_overlay_refresh_tool_contract() -> None:
    """验证海克斯视觉资源更新工具的持久样本与发布边界。"""

    refresh_tool = importlib.import_module("tooling.setup.vision")
    parser = refresh_tool.build_parser()
    assert parser.parse_args(["--check-only"]).check_only is True

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        truth_path = root / "tests" / "fixtures" / "diagnostics" / "truth.json"
        fixture_path = root / "tests" / "fixtures" / "diagnostics" / "overlay_vision_fixtures" / "case" / "name_0.png"
        fixture_path.parent.mkdir(parents=True)
        fixture_path.write_bytes(b"fixture")
        truth_path.parent.mkdir(parents=True, exist_ok=True)
        truth_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "samples": [],
                    "name_roi_samples": [
                        {
                            "id": "durable",
                            "name_crops": [str(fixture_path.relative_to(root))],
                            "expected_names": ["尤里卡"],
                        }
                    ],
                    "retired_samples": [{"id": "lost", "reason": "source_frame_missing_before_gate"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        truth_summary = refresh_tool.validate_truth_manifest(truth_path, run_dir=root)
        assert truth_summary["active_sample_count"] == 1
        assert truth_summary["full_frame_sample_count"] == 0
        assert truth_summary["name_roi_sample_count"] == 1
        assert truth_summary["retired_sample_count"] == 1
        assert truth_summary["missing_count"] == 0
        compact_truth = refresh_tool._compact_summary(truth_summary)
        assert compact_truth["full_frame_sample_count"] == 0
        assert compact_truth["name_roi_sample_count"] == 1

        bad_truth_path = truth_path.with_name("bad.json")
        bad_truth_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "samples": [{"id": "runtime", "frame": "var/reports/frame.png", "expected_slots": []}],
                    "name_roi_samples": [],
                }
            ),
            encoding="utf-8",
        )
        bad_summary = refresh_tool.validate_truth_manifest(bad_truth_path, run_dir=root)
        assert bad_summary["invalid_path_count"] == 1

        catalog_summary = {
            "missing_field_count": 0,
            "duplicate_stable_id_count": 0,
            "missing_icon_count": 0,
            "invalid_icon_count": 0,
        }
        audit_summary = {"missing_identity_count": 0, "missing_variant_count": 0}
        fixture_summary = {
            "full_frame_sample_count": 0,
            "missing_count": 1,
            "invalid_path_count": 0,
            "fixture_missing_count": 0,
            "fixture_failure_count": 0,
        }
        with (
            patch.object(refresh_tool, "validate_official_catalog", return_value=catalog_summary),
            patch.object(refresh_tool.overlay_vision_sidecar, "audit_default_template_index", return_value=audit_summary),
            patch.object(refresh_tool, "run_synthetic_recognition", return_value={"synthetic_failure_count": 0}),
            patch.object(refresh_tool, "run_fixture_regression", return_value=fixture_summary),
        ):
            blocked = refresh_tool.validate_snapshot(root)
        assert blocked["passed"] is False
        assert blocked["blockers"]["truth_missing_count"] == 1
        assert blocked["blockers"]["full_frame_sample_deficit"] == refresh_tool.MIN_FULL_FRAME_SAMPLE_COUNT

        snapshot = root / "snapshot"
        target = root / "target"
        snapshot_icon = snapshot / "resources" / "assets" / "augments" / "new.png"
        snapshot_catalog = snapshot / "resources" / "catalog" / "海克斯资源目录.v1.json"
        snapshot_icon.parent.mkdir(parents=True)
        snapshot_catalog.parent.mkdir(parents=True)
        snapshot_icon.write_bytes(b"new-icon")
        snapshot_catalog.write_text('{"schema_version": 1}', encoding="utf-8")
        stale_icon = target / "resources" / "assets" / "augments" / "stale.png"
        stale_icon.parent.mkdir(parents=True)
        stale_icon.write_bytes(b"keep")

        published = refresh_tool.publish_snapshot(snapshot, target_run_dir=target)
        assert published["icon_count"] == 1
        assert stale_icon.read_bytes() == b"keep"
        assert (target / "resources" / "assets" / "augments" / "new.png").read_bytes() == b"new-icon"
        assert (target / "resources" / "catalog" / "海克斯资源目录.v1.json").exists()

        cache_root = root / "var" / "cache" / "vision"
        with (
            patch.object(refresh_tool, "RUN_DIR", root),
            patch.object(refresh_tool, "build_refresh_snapshot", return_value={"built": True}),
            patch.object(refresh_tool, "validate_snapshot", return_value={"passed": True}),
            patch.object(refresh_tool, "publish_snapshot", return_value={"icon_count": 1}),
        ):
            assert refresh_tool.main(["--json"]) == 0
        assert not list(cache_root.glob("overlay_recognition_refresh_*"))

        cleanup_output = io.StringIO()
        with (
            patch.object(refresh_tool, "RUN_DIR", root),
            patch.object(refresh_tool, "build_refresh_snapshot", return_value={"built": True}),
            patch.object(refresh_tool, "validate_snapshot", return_value={"passed": True}),
            patch.object(refresh_tool, "publish_snapshot", return_value={"icon_count": 1}),
            patch.object(refresh_tool.shutil, "rmtree", side_effect=PermissionError("snapshot busy")),
            patch.object(sys, "stdout", cleanup_output),
        ):
            assert refresh_tool.main(["--json"]) == 0
        cleanup_summary = json.loads(cleanup_output.getvalue())
        assert cleanup_summary["snapshot_retained"] is True
        assert "snapshot busy" in cleanup_summary["snapshot_cleanup_error"]
        cleanup_compact = refresh_tool._compact_summary(cleanup_summary)
        assert cleanup_compact["snapshot_retained"] is True
        assert "snapshot busy" in cleanup_compact["snapshot_cleanup_error"]

    refresh_source = Path(refresh_tool.__file__).read_text(encoding="utf-8")
    assert "overlay_vision_sidecar._render_name_mask" not in refresh_source
    assert "overlay_vision_sidecar._rank_matrices" not in refresh_source
    assert callable(refresh_tool.overlay_vision_sidecar.render_name_mask)
    assert callable(refresh_tool.overlay_vision_sidecar.rank_template_matrices)
