"""验证 generation 与来源 current 的完整性。

默认验证可随包分发的 seed generation。传入 ``--runtime`` 时验证当前 ``var``
generation；``--require-sources`` 进一步要求 Hextech、Apex、Mayhem 三个来源 current
均存在且 artifact 哈希有效。本工具只读，不触发抓取或发布。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.data.source_runs import KNOWN_SOURCES, load_source_current
from hextech.modules.data.generation import DataSnapshotClient, SnapshotValidationError


class AcceptanceFailure(RuntimeError):
    """验收证据缺失或跨 generation/session 不一致。"""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"验收 JSON 无法读取：{path}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceFailure(f"验收 JSON 必须是对象：{path}")
    return payload


def verify_real_session_evidence(path: Path, *, expected_generation_id: str) -> dict[str, Any]:
    """验证 LCU、窗口、Vision、推荐、渲染和截图属于同一真实会话。"""

    payload = _read_object(path)
    if payload.get("evidence_kind") != "real_game_session":
        raise AcceptanceFailure("缺少真实游戏会话证据标记")
    generation_id = str(payload.get("generation_id") or "")
    session_id = str(payload.get("session_id") or "")
    if generation_id != expected_generation_id:
        raise AcceptanceFailure("真实会话 generation 与已验证快照不一致")
    if not session_id:
        raise AcceptanceFailure("真实会话缺少 session_id")
    sections: dict[str, dict[str, Any]] = {}
    for name in ("lcu", "window", "vision", "recommendation", "final_state"):
        section = payload.get(name)
        if not isinstance(section, dict):
            raise AcceptanceFailure(f"真实会话缺少 {name} 证据")
        if str(section.get("session_id") or "") != session_id:
            raise AcceptanceFailure(f"真实会话 session 不一致：{name}")
        sections[name] = section
    if not str(sections["lcu"].get("local_champion_id") or ""):
        raise AcceptanceFailure("真实 LCU 未取得本地英雄")
    window = sections["window"]
    if int(window.get("hwnd") or 0) <= 0 or not window.get("client_size") or not window.get("capture_size"):
        raise AcceptanceFailure("真实游戏窗口证据不完整")
    if float(window.get("dpi_scale") or 0) <= 0:
        raise AcceptanceFailure("真实游戏窗口 DPI 证据无效")
    vision = sections["vision"]
    if int(vision.get("epoch") or 0) <= 0 or len(vision.get("slots") or []) != 3:
        raise AcceptanceFailure("真实 Vision epoch 或三槽证据不完整")
    final_state = sections["final_state"]
    if str(sections["recommendation"].get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实推荐 generation 不一致")
    if str(final_state.get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实渲染 generation 不一致")
    if int(final_state.get("vision_epoch") or 0) != int(vision.get("epoch") or 0):
        raise AcceptanceFailure("真实渲染 Vision epoch 不一致")
    if not final_state.get("should_show") or final_state.get("presentation_mode") != "content":
        raise AcceptanceFailure("真实 Overlay 最终未进入可见内容态")
    screenshot = Path(str(payload.get("screenshot") or ""))
    screenshot = screenshot if screenshot.is_absolute() else path.parent / screenshot
    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
        raise AcceptanceFailure("真实 Overlay 非空截图缺失")
    return {
        "generation_id": generation_id,
        "session_id": session_id,
        "vision_epoch": int(vision["epoch"]),
        "screenshot": str(screenshot),
    }


def verify_generation(root: Path) -> dict[str, Any]:
    """打开固定 generation，并交叉验证 manifest 计数和查询结果。"""

    view = DataSnapshotClient(root).open_view()
    champions = view.get_champions()
    details = [view.get_champion_detail(item["id"]) for item in champions]
    stat_record_count = sum(
        len(detail.get("augments", []))
        for detail in details
        if isinstance(detail, dict) and isinstance(detail.get("augments"), list)
    )
    manifest = view.manifest
    if len(champions) != manifest.champion_count:
        raise SnapshotValidationError("英雄数量与 generation manifest 不一致")
    if stat_record_count != manifest.stat_record_count:
        raise SnapshotValidationError("统计记录数与 generation manifest 不一致")
    return {
        "state": view.status()["state"],
        "generation_id": manifest.generation_id,
        "champion_count": manifest.champion_count,
        "augment_count": manifest.augment_count,
        "stat_record_count": manifest.stat_record_count,
    }


def verify_sources() -> dict[str, Any]:
    """要求全部来源 current 可解析且其 artifact 哈希匹配。"""

    result: dict[str, Any] = {}
    missing: list[str] = []
    for source in sorted(KNOWN_SOURCES):
        pointer = load_source_current(source, verify_hash=True)
        if not pointer:
            missing.append(source)
            result[source] = {"state": "unavailable"}
            continue
        result[source] = {
            "state": "ready",
            "run_id": pointer["run_id"],
            "record_count": pointer["record_count"],
            "sha256": pointer["sha256"],
        }
    if missing:
        raise RuntimeError(f"来源 current 缺失或无效：{', '.join(missing)}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 Hextech 完整 generation 和来源 current。")
    parser.add_argument("--runtime", action="store_true", help="验证 var/snapshots，而不是 resources/seeds。")
    parser.add_argument("--require-sources", action="store_true", help="同时要求三个来源 current 和 artifact 有效。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot_root = RUN_DIR / ("var/snapshots" if args.runtime else "resources/seeds")
    try:
        summary: dict[str, Any] = {"generation": verify_generation(snapshot_root)}
        if args.require_sources:
            summary["sources"] = verify_sources()
        summary["passed"] = True
    except (OSError, RuntimeError, SnapshotValidationError) as exc:
        summary = {"passed": False, "error": str(exc)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
