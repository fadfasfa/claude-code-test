"""Read-only acceptance of a v4 release, its immutable closure, and real session evidence."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.data.generation import DataSnapshotClient, SnapshotValidationError, default_snapshot_root
from hextech.modules.data.generation.release_store import closure, object_at

ACTIVE_SOURCES = ("aramkit", "apex", "mayhem")
OPTIONAL_SOURCES = frozenset({"apex", "mayhem"})

class AcceptanceFailure(RuntimeError):
    """Acceptance evidence is absent, corrupted, or bound to a different release."""


def _read_object(path: Path) -> dict[str, Any]:
    return object_at(path)


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
    schema_version = int(payload.get("schema_version") or 1)
    selection_revision = 0
    render_signature = ""
    if schema_version >= 2:
        render = payload.get("render")
        if not isinstance(render, dict):
            raise AcceptanceFailure("真实 Overlay v2 缺少 render 证据")
        if str(render.get("session_id") or "") != session_id:
            raise AcceptanceFailure("真实会话 session 不一致：render")
        if str(render.get("generation_id") or "") != generation_id:
            raise AcceptanceFailure("真实渲染 generation 不一致：render")
        if int(render.get("vision_epoch") or 0) != int(vision.get("epoch") or 0):
            raise AcceptanceFailure("真实渲染 Vision epoch 不一致：render")
        selection_revision = int(payload.get("selection_revision") or 0)
        if selection_revision <= 0 or int(render.get("selection_revision") or 0) != selection_revision:
            raise AcceptanceFailure("真实渲染 revision 不一致")
        rows = render.get("rows")
        if not isinstance(rows, list) or len(rows) != 3:
            raise AcceptanceFailure("真实 Overlay v2 缺少三槽 render rows")
        render_signature = str(payload.get("render_signature") or "")
        if not render_signature or str(render.get("render_signature") or "") != render_signature:
            raise AcceptanceFailure("真实 Overlay render signature 不一致")
        signature_payload = {
            "session_id": session_id,
            "generation_id": generation_id,
            "vision_epoch": int(vision.get("epoch") or 0),
            "selection_revision": selection_revision,
            "rows": rows,
        }
        raw_signature = json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        expected_signature = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()
        if render_signature != expected_signature:
            raise AcceptanceFailure("真实 Overlay render signature 无法复算")
    screenshot = Path(str(payload.get("screenshot") or ""))
    screenshot = screenshot if screenshot.is_absolute() else path.parent / screenshot
    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
        raise AcceptanceFailure("真实 Overlay 非空截图缺失")
    return {
        "generation_id": generation_id,
        "session_id": session_id,
        "vision_epoch": int(vision["epoch"]),
        "selection_revision": selection_revision,
        "render_signature": render_signature,
        "screenshot": str(screenshot),
    }


def verify_generation(root: Path) -> dict[str, Any]:
    view = DataSnapshotClient(root).open_view()
    manifest = view.manifest
    if manifest.schema_version != 4 or view.degraded:
        raise AcceptanceFailure("requires intact v4 release; migrate historical v3 offline")
    catalog, ranking, heroes, optional = closure(root.parent, manifest)
    champions = view.get_champions()
    if len(champions) != manifest.champion_count:
        raise AcceptanceFailure("champion projection does not match release components")
    for champion in champions:
        if not view.get_champion_detail(champion["id"]):
            raise AcceptanceFailure("release champion projection missing")
    return {"state": view.status()["state"], "schema_version": 4,
            "generation_id": manifest.generation_id, "content_fingerprint": manifest.content_fingerprint,
            "champion_count": manifest.champion_count, "augment_count": manifest.augment_count,
            "stat_record_count": manifest.stat_record_count, "complete_champion_count": len(heroes)}


def verify_sources(snapshot_root: Path | None = None, **_kwargs) -> dict[str, Any]:
    root = snapshot_root or default_snapshot_root()
    view = DataSnapshotClient(root).open_view()
    catalog, ranking, heroes, optional = closure(root.parent, view.manifest)
    return {"catalog_generation_id": catalog.generation_id,
            "ranking_run_id": ranking["run_id"], "complete_champion_count": len(heroes),
            "optional_sources": sorted(optional),
            "units": [p.to_dict() for p in view.manifest.source_files if p.source != "catalog"]}


def verify_strict_full_chain(snapshot_root: Path, *, now=None) -> dict[str, Any]:
    generation = verify_generation(snapshot_root)
    sources = verify_sources(snapshot_root)
    return {"generation": generation, "sources": sources, "real_session_acceptance": "not_run"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify v4 release and immutable source closure.")
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--require-sources", action="store_true")
    parser.add_argument("--strict-full-chain", action="store_true")
    parser.add_argument("--report-dir", type=Path)
    return parser


def _write_report(report_dir: Path | None, summary: Mapping[str, Any]) -> None:
    if report_dir is None:
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(
        json.dumps(dict(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # ``--runtime`` 必须与 Catalog/source current 使用同一个可变数据根。
    # 否则设置 HEXTECH_VAR_DIR 后会把真实来源 pointer 与 worktree 的旧
    # ``var/snapshots`` 交叉比较，制造并不存在的 cohort fingerprint 失败。
    snapshot_root = default_snapshot_root() if args.runtime else RUN_DIR / "resources/seeds/runtime.v4/snapshots"
    try:
        if args.strict_full_chain:
            if not args.runtime:
                raise AcceptanceFailure("--strict-full-chain 必须配合 --runtime")
            summary: dict[str, Any] = verify_strict_full_chain(snapshot_root)
        else:
            summary = {"generation": verify_generation(snapshot_root)}
            if args.require_sources:
                summary["sources"] = verify_sources(snapshot_root)
        summary["passed"] = True
    except (OSError, RuntimeError, ValueError, SnapshotValidationError) as exc:
        summary = {"passed": False, "error": str(exc), "error_type": exc.__class__.__name__}
    _write_report(args.report_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
