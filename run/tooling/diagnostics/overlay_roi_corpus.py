"""验证本机 Overlay 五类 ROI corpus，不复制或修改任何像素。

清单位于 ignored ``run/var``，只记录人工标签、来源、路径和文件 SHA-256。工具
只允许读取当前 worktree 的公开 fixture、冻结私有 corpus 或 HextechNexus 私有
debug 根；路径越界、文件漂移、分类缺失、碎片误拦截、运行时报告缺失或非普通
场景出现 READY 都会 fail closed。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from hextech.infrastructure.vision.sidecar_fingerprints import (
    _body_shard_name_scores,
    _body_shard_scene_present,
)
from hextech.modules.data.ports.paths import PROJECT_ROOT, get_var_dir


CORPUS_SCHEMA_VERSION = 1
REQUIRED_CLASSES = {"hextech", "body_shard", "animation", "fade", "invalid_crop"}
DEFAULT_MANIFEST = get_var_dir() / "recognition" / "corpus" / "overlay-private-corpus.v1.json"


def _allowed_roots() -> tuple[Path, ...]:
    roots = [
        (PROJECT_ROOT / "tests" / "fixtures" / "diagnostics").resolve(),
        (get_var_dir() / "recognition" / "corpus" / "samples").resolve(),
    ]
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        roots.append((Path(local_app_data) / "HextechNexus" / "var" / "debug").resolve())
    return tuple(roots)


def _within_allowed_root(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in _allowed_roots())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_private_corpus(manifest_path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) != CORPUS_SCHEMA_VERSION:
        raise ValueError("私有 ROI corpus schema_version 不受支持")
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    labels = {str(sample.get("class") or "") for sample in samples if isinstance(sample, Mapping)}
    failures: list[dict[str, Any]] = []
    if labels != REQUIRED_CLASSES:
        failures.append({"kind": "class_set_mismatch", "expected": sorted(REQUIRED_CLASSES), "observed": sorted(labels)})

    results: list[dict[str, Any]] = []
    runtime_report_classes: set[str] = set()
    for sample in samples:
        if not isinstance(sample, Mapping):
            failures.append({"kind": "sample_not_object"})
            continue
        sample_id = str(sample.get("id") or "")
        label = str(sample.get("class") or "")
        evidence_source = str(sample.get("evidence_source") or "")
        root = Path(str(sample.get("root") or ""))
        if not root.is_absolute():
            root = path.parent / root
        sample_failures: list[str] = []
        if evidence_source not in {"private_frozen", "tracked_fixture"}:
            sample_failures.append("evidence_source_invalid")
        if evidence_source == "tracked_fixture" and label != "body_shard":
            sample_failures.append("tracked_fixture_only_allowed_for_body_shard")
        if not _within_allowed_root(root):
            sample_failures.append("path_outside_allowed_roots")
        if not root.is_dir():
            sample_failures.append("sample_root_missing")
        files = sample.get("files") if isinstance(sample.get("files"), list) else []
        for item in files:
            if not isinstance(item, Mapping):
                sample_failures.append("file_entry_not_object")
                continue
            target = root / str(item.get("name") or "")
            if not target.is_file():
                sample_failures.append(f"file_missing:{target.name}")
                continue
            if int(item.get("size") or -1) != target.stat().st_size:
                sample_failures.append(f"file_size_mismatch:{target.name}")
            if str(item.get("sha256") or "").lower() != _sha256(target):
                sample_failures.append(f"file_sha256_mismatch:{target.name}")

        name_paths = sorted(root.glob("name_[0-2].png"))
        scores: tuple[float, ...] = ()
        body_shard = False
        if name_paths:
            with_images = [Image.open(name_path).convert("RGB") for name_path in name_paths]
            scores = _body_shard_name_scores(with_images)
            body_shard = _body_shard_scene_present(scores)
        if label == "body_shard" and not body_shard:
            sample_failures.append("body_shard_false_negative")
        if label != "body_shard" and body_shard:
            sample_failures.append("body_shard_false_positive")

        report_path = root / "report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            source = report.get("source") if isinstance(report.get("source"), Mapping) else {}
            ready_slots = int(source.get("ready_slots") or 0)
            runtime_report_classes.add(label)
            if label in {"body_shard", "animation", "fade", "invalid_crop"} and ready_slots != 0:
                sample_failures.append(f"false_ready:{ready_slots}")
        else:
            sample_failures.append("report_missing")

        if sample_failures:
            failures.append({"kind": "sample_failed", "id": sample_id, "details": sample_failures})
        results.append(
            {
                "id": sample_id,
                "class": label,
                "evidence_source": evidence_source,
                "body_shard_scores": list(scores),
                "body_shard": body_shard,
                "passed": not sample_failures,
            }
        )
    missing_runtime_report_classes = REQUIRED_CLASSES - runtime_report_classes
    return {
        "schema_version": 1,
        "manifest": str(path.resolve()),
        "required_classes": sorted(REQUIRED_CLASSES),
        "sample_count": len(results),
        "samples": results,
        "runtime_report_classes": sorted(runtime_report_classes),
        "missing_runtime_report_classes": sorted(missing_runtime_report_classes),
        "failures": failures,
        "passed": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证本机 Overlay 五类私有 ROI corpus。")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    result = validate_private_corpus(args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
