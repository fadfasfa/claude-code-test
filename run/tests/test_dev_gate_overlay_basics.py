"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (Path, RUN_DIR, TemporaryDirectory, patch)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_overlay_performance_probe_contract() -> None:
    """验证游戏内显示性能记录结构可用于阶段 5 手动验收。"""
    import tooling.acceptance.overlay_performance_probe as overlay_performance_probe

    sample = overlay_performance_probe.build_overlay_performance_report(
        service_samples={
            "all_off": {"rss_mb": 90.0, "cpu_percent": 0.2},
            "app_only": {"rss_mb": 145.0, "cpu_percent": 1.4},
            "app_host_vision": {"rss_mb": 130.0, "cpu_percent": 2.0},
        },
        latency_samples_ms=[120.0, 240.0, 510.0],
        source_tag="dev-check",
    )
    assert sample["source"]["tag"] == "dev-check"
    assert set(sample["service_states"]) == {
        "all_off",
        "app_only",
        "app_host_vision",
    }
    assert sample["latency"]["p50_ms"] == 240.0
    assert sample["latency"]["p95_ms"] == 510.0
    assert sample["targets"]["recognition_p95_ms"] == 180.0
    assert sample["targets"]["overlay_p95_ms"] == 900.0
    assert sample["targets"]["render_p95_ms"] == 100.0
    assert sample["manual_acceptance_required"] is True

    module_text = (RUN_DIR / "tooling" / "acceptance" / "overlay_performance_probe.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "data" + "/runtime" not in module_text

def test_game_overlay_documentation_contract() -> None:
    """验证当前系统设计入口、正式路线和启动降级口径。"""

    readme_text = (RUN_DIR / "README.md").read_text(encoding="utf-8")
    project_text = (RUN_DIR / "docs" / "system-design.md").read_text(encoding="utf-8")
    docs_index_text = (RUN_DIR / "docs" / "README.md").read_text(encoding="utf-8")
    design_text = (RUN_DIR / "docs" / "system-design.md").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe -m hextech.infrastructure.vision.sidecar --once --preset auto --write-event" in readme_text
    assert ".venv\\Scripts\\python.exe -m hextech.infrastructure.vision.sidecar --loop --preset auto --write-event" in readme_text
    assert "# Hextech 伴生系统设计" in project_text
    assert "src/hextech" in project_text
    assert "var/snapshots" in project_text
    assert "DataService" in project_text
    assert "system-design.md" in docs_index_text
    assert "data-layout.md" in docs_index_text
    assert "generation" in design_text


def test_overlay_runtime_paths_contract() -> None:
    """验证 overlay event/context 共享的轻量运行态路径规则。"""
    import hextech.modules.vision.runtime_paths as overlay_runtime_paths

    with TemporaryDirectory() as tmp_dir:
        var_root = Path(tmp_dir) / "var"
        with patch.object(overlay_runtime_paths, "get_var_dir", return_value=var_root):
            resolved = Path(overlay_runtime_paths.overlay_runtime_state_path("probe.json"))
            assert resolved == (var_root / "state" / "probe.json").resolve()
            try:
                overlay_runtime_paths.overlay_runtime_state_path("../escaped.json")
            except ValueError as exc:
                assert "escaped state dir" in str(exc)
            else:
                raise AssertionError("overlay runtime state 路径不得逃逸 state 根目录")
