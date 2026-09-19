"""Clone only verified public data, then probe sources and publish in that isolated clone."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def clone(source: Path, target: Path) -> None:
    if target.exists():
        raise ValueError("verification target already exists")
    os.environ["HEXTECH_VAR_DIR"] = str(source)
    from tooling.build.cohort_seed import collect_cohort_seed
    seed = collect_cohort_seed(source / "snapshots")
    generation = str(seed.metadata["generation_id"])
    target.mkdir(parents=True)
    for original in seed.files:
        relative = original.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    (target / "snapshots").mkdir(exist_ok=True)
    shutil.copy2(source / "snapshots/current.v2.json", target / "snapshots/current.v2.json")
    shutil.copytree(source / "snapshots/generations" / generation,
                    target / "snapshots/generations" / generation)


def verify(root: Path) -> dict:
    os.environ["HEXTECH_VAR_DIR"] = str(root)
    from hextech.infrastructure.sources.aramkit import service as aram
    from hextech.infrastructure.sources.aramkit.schema import normalize_rankings, normalize_detail
    from hextech.infrastructure.sources.mayhem.service import run_mayhem_refresh
    from hextech.infrastructure.sources.refresh_service import IncrementalRefreshService
    from hextech.modules.data.generation import DataSnapshotPublisher, DataSnapshotClient
    from hextech.modules.data.ports.atomic import atomic_write_json
    result: dict = {"runtime": str(root), "real_game_qualified": False}
    try:
        marker = aram.probe_aramkit_upstream_marker(conditional_cache_root=root / "probe-cache")
        base = f"{aram.DATA_BASE_URL}/{marker['dataPath']}/stats/all"
        budget = aram._ByteBudget(per_response=aram.MAX_RESPONSE_BYTES, total=aram.MAX_TOTAL_BYTES)
        rows = normalize_rankings(aram._require_json_response(aram._default_fetcher,
            f"{base}/champion-rankings.json", budget, context="rankings"))
        row = next(item for item in rows if str(item["id"]) == "38")
        detail = normalize_detail(aram._require_json_response(aram._default_fetcher,
            f"{base}/champion-details/38.json", budget, context="champion38"), row)
        result["aramkit"] = {"success": True, "marker": marker, "champions": len(rows),
                             "champion_id": "38", "augment_sections": list(detail["augments"])}
    except Exception as exc:
        result["aramkit"] = {"success": False, "error": str(exc)}
    pointer_path = root / "mayhem-candidate.json"
    mayhem = run_mayhem_refresh(force=True, pointer_output=pointer_path,
                                conditional_cache_root=root / "probe-cache")
    result["mayhem"] = {key: mayhem.get(key) for key in (
        "success", "reason", "raw_items", "added_items", "run_id", "failure_kind", "check_status")}
    if mayhem.get("success") and pointer_path.exists():
        try:
            coordinator = IncrementalRefreshService(root=root, publisher=DataSnapshotPublisher(root / "snapshots"),
                game_state_probe=lambda: False, champion_probe=lambda: "38")
            coordinator._optional["mayhem"] = json.loads(pointer_path.read_text(encoding="utf-8"))
            generation = coordinator._publish()
            coordinator._mark_source("mayhem", result=mayhem)
            view = DataSnapshotClient(root / "snapshots").open_view()
            hints = view.get_overlay_display_hints()
            sources = sorted({item.source for item in view.manifest.source_files})
            if "blitz" in sources or not view.is_champion_complete("38"):
                raise ValueError("new snapshot source/completeness contract failed")
            result["projection"] = {"success": True, "generation_id": generation, "sources": sources,
                "champions": len(view.get_champions()), "synergy_projection": hints.get("source", {}).get("synergy_projection")}
        except Exception as exc:
            result["projection"] = {"success": False, "error": str(exc)}
    atomic_write_json(root / "verification.json", result, ensure_ascii=False, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-clone", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    artifacts = Path(__file__).resolve().parents[2] / ".artifacts"
    if artifacts.resolve() not in output.parents:
        parser.error("output must be a new runtime beneath run/.artifacts")
    if args.verify_clone:
        result = verify(output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(result.get(key, {}).get("success") for key in ("aramkit", "mayhem", "projection")) else 2)
    if args.source_root is None:
        parser.error("source-root required to clone")
    clone(args.source_root.resolve(), output)
    completed = subprocess.run([sys.executable, "-m", "tooling.acceptance.verify_unified_repair",
                                "--output", str(output), "--verify-clone"], check=False,
                               env={**os.environ, "HEXTECH_VAR_DIR": str(output)})
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
