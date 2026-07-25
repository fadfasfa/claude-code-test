"""锁定 Overlay 长期手册的发现链和运行契约，避免 CC 再使用旧部署口径。"""

from __future__ import annotations

from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RUN_ROOT.parent


def test_overlay_runtime_document_discovery_chain_exists() -> None:
    claude_entry = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    root_index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    run_index = (RUN_ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert "工作区的本地文档索引" in claude_entry
    assert "run/docs/README.md" in root_index
    assert "overlay-runtime.md" in run_index
    assert (RUN_ROOT / "docs" / "overlay-runtime.md").is_file()


def test_overlay_runtime_document_matches_code_contracts() -> None:
    from hextech.modules.session.build_identity import RUNTIME_CONTRACT_VERSIONS as RUNTIME_VERSIONS
    from tooling.build.manifest import (
        BUNDLE_MANIFEST_SCHEMA_VERSION,
        RUNTIME_CONTRACT_VERSIONS as BUILD_VERSIONS,
    )

    handbook = (RUN_ROOT / "docs" / "overlay-runtime.md").read_text(encoding="utf-8")
    assert BUILD_VERSIONS == RUNTIME_VERSIONS == {
        "overlay_event": 3,
        "sidecar_status": 2,
        "overlay_session_report": 2,
    }
    assert BUNDLE_MANIFEST_SCHEMA_VERSION == 3
    for phrase in (
        "bundle manifest | v3",
        "Overlay event | v3",
        "Sidecar status | v2",
        "Overlay session report | v2",
        "P95 ≤ 300 ms",
        "P95 ≤ 900 ms",
        "默认模式：不生成 PNG",
        "对局结束后等待 30 秒",
        "compute_profile=float32_batched",
        "`strong` 在最近 3 个有效观察中同身份命中 2 次",
        "`medium` 在最近 5 个有效观察中命中 3 次",
        "evidence_starved",
        "已有部分稳定槽且仍有待识别槽时保留 6 秒",
        "三槽均 ready 时保留 1.5 秒",
        "scene_loss_confirmed",
        "投影覆盖至少 99%",
        "单帧捕获加识别 P95 ≤ 180 ms",
        "右上角“×”",
        "连续 300 秒",
        "desktop_ui_activation.v1.json",
        "CREATE_NO_WINDOW",
        "识别已休眠",
    ):
        assert phrase in handbook
