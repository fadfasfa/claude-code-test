"""真实数据链验收器的纯函数与失败门禁测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tools.acceptance.verify_data_pipeline import (
    AcceptanceFailure,
    _assert_display_metric,
    _card_summary,
    _verify_nonblank_image,
    select_acceptance_samples,
)


class SnapshotView:
    def get_champions(self) -> list[dict]:
        return [{"id": "1", "name": "英雄一"}]

    def get_champion_augments(self, _hero: object) -> list[dict]:
        return [
            {
                "id": "a1",
                "name": "强化一",
                "海克斯胜率": 0.61,
                "海克斯出场率": 0.04,
            }
        ]

    def get_overlay_hints(self) -> dict:
        return {
            "hints": {
                "a1": {"name": "强化一"},
                "a2": {"name": "强化二"},
            }
        }


def test_sample_selection_preserves_real_stats_and_finds_real_no_stats_combo() -> None:
    sample = select_acceptance_samples(SnapshotView())

    assert sample["hero_id"] == "1"
    assert sample["card_summary"] == {
        "id": "a1",
        "name": "强化一",
        "win_rate": 0.61,
        "pick_rate": 0.04,
    }
    assert sample["no_stats_card"] == {"id": "a2", "name": "强化二"}


def test_card_summary_normalizes_percent_strings() -> None:
    assert _card_summary(
        {
            "海克斯ID": "a1",
            "海克斯名称": "强化一",
            "海克斯胜率": "61%",
            "海克斯出场率": "4%",
        }
    ) == {"id": "a1", "name": "强化一", "win_rate": 0.61, "pick_rate": 0.04}


def test_overlay_display_metric_uses_renderer_precision() -> None:
    _assert_display_metric("62.1%", 0.621163, "Overlay 胜率")
    with pytest.raises(AcceptanceFailure, match="显示不一致"):
        _assert_display_metric("62.0%", 0.621163, "Overlay 胜率")


def test_screenshot_gate_rejects_solid_image_and_accepts_visible_content(tmp_path: Path) -> None:
    solid = tmp_path / "solid.png"
    Image.new("RGB", (640, 360), "black").save(solid)
    with pytest.raises(AcceptanceFailure, match="像素无有效内容"):
        _verify_nonblank_image(solid)

    visible = tmp_path / "visible.png"
    image = Image.new("RGB", (640, 360), "black")
    ImageDraw.Draw(image).rectangle((20, 20, 200, 100), fill="white")
    image.save(visible)
    _verify_nonblank_image(visible)


def test_refresh_and_verified_snapshot_build_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    from tools.build_package import parse_build_args

    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    with pytest.raises(SystemExit):
        parse_build_args(["--refresh-data", "--verified-snapshot-root", str(snapshot_root)])


def test_direct_acceptance_script_adds_run_dir_before_late_imports(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "acceptance" / "verify_data_pipeline.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({str(script)!r}); import hextech; print(hextech.__file__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert str(script.parents[2] / "hextech") in completed.stdout
