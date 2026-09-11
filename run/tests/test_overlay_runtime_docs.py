"""锁定 Overlay 长期手册的发现链和运行契约，避免 CC 再使用旧部署口径。"""

from __future__ import annotations

from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RUN_ROOT.parent


def test_overlay_runtime_document_discovery_chain_exists() -> None:
    claude_entry = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    root_index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    run_index = (RUN_ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert "本地文档索引" in claude_entry
    assert "run/docs/README.md" in root_index
    assert "overlay-runtime.md" in run_index
    assert "overlay-recurrent-issues.md" in run_index
    assert (RUN_ROOT / "docs" / "overlay-runtime.md").is_file()
    assert (RUN_ROOT / "docs" / "overlay-recurrent-issues.md").is_file()


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
        "P95 ≤ 900 ms",
        "Host event→present P95 ≤ 100 ms",
        "默认模式：不生成 PNG",
        "对局结束后等待 30 秒",
        "compute_profile=float32_batched",
        "`strong` 在最近 3 个原始观察中同身份命中 2 次",
        "`medium` 在最近 5 个原始观察中命中 3 次",
        "双方 Top-3 中存在唯一共同身份",
        "evidence_starved",
        "统一 0.75 秒真实时间宽限",
        "scene_loss_confirmed",
        "只有唯一 `strong` 候选可以进入时序窗口",
        "每段可见文字只创建一个 Canvas text item",
        "共享 `var/locks` 下的 OS 独占文件锁",
        "投影覆盖至少 99%",
        "单帧捕获加识别 P95 ≤ 180 ms",
        "slot_generation",
        "两个不同 `frame_id`",
        "最右两个相邻",
        "Host 按 session、selection epoch/revision",
        "automatic_exemplar_eligible=false",
        "5 局真机稳定门",
        "右上角“×”",
        "连续 300 秒",
        "desktop_ui_activation.v1.json",
        "CREATE_NO_WINDOW",
        "识别已休眠",
        "off/observe/admit",
        "confidence >= 0.95",
        "ocr_exact_fallback",
        "runtime_restored",
        "统计数据暂非最新",
        "普通模板候选或感知指纹漂移没有换卡授权",
        "Vision timeline | v2",
        "catalog_adoption_checkpoint.v1.json",
        "optional_source_stale",
        "约每 10 ms 捕获左键物理 down-edge",
        "populated-runtime",
        "build-aware v2",
        "pending-process registry",
        "game_window_mode",
        "probe_contract=dwm_desktop_dc",
        "WindowMode=0/1/2",
        "diagnostic_retention` 是所有诊断删除/轮转的唯一所有者",
        "共享最短运行间隔 60 秒",
        "不占 v2 数量上限和 12 MiB 分类预算",
        "selection_type=hextech",
        "excluded_epochs_by_reason",
        "Overlay 矩形裁剪图",
        "timeline_missing=0",
        "私人本机实验",
        "公开分发阻塞项",
    ):
        assert phrase in handbook


def test_overlay_recurrent_issues_contains_v5_prevention_checklist() -> None:
    history = (RUN_ROOT / "docs" / "overlay-recurrent-issues.md").read_text(encoding="utf-8")
    for phrase in (
        "2026-08-31",
        "点击快捷方式不等于切换 Build",
        "legacy/current schema",
        "同一资源只能有一个清理所有者",
        "实际 append 成功",
        "candidate/body-shard epoch",
        "capture+recognition P95",
        "Augment/Arena win-rate overlay",
    ):
        assert phrase in history


def test_current_desktop_contract_and_capture_repair_are_not_contradictory():
    handbook = (RUN_ROOT / "docs" / "overlay-runtime.md").read_text(encoding="utf-8")
    repair = (RUN_ROOT / "docs" / "overlay-display-repair-v3.md").read_text(encoding="utf-8")
    assert "唯一以 [desktop-stable28.md]" in handbook
    assert "不足时尝试右侧物理相邻屏" not in handbook
    assert "不接受独立手动位置，不跳邻屏" in handbook
    for contract in ("mss==10.2.0", "capture_regions_valid", "HeldSceneEvidence", "mss_capture_exclusion_v1"):
        assert contract in repair
