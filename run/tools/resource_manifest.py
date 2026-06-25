from __future__ import annotations

"""资源中文分类清单校验。

本模块只验证 `resources/资源清单.v1.json` 的结构和路径边界，不移动资源文件。
它让中文资源事实源可审计，并阻止运行态输出混入稳定资源。
"""

import json
from pathlib import Path
from typing import Any


MANIFEST_RELATIVE_PATH = Path("resources") / "资源清单.v1.json"
RAW_ALLOWED_KINDS = {"source-evidence", "startup-snapshot"}


def _as_relative_posix(path_text: str) -> str:
    path = Path(str(path_text or ""))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"资源清单路径必须是仓库内相对路径：{path_text}")
    return path.as_posix().strip("/")


def load_resource_manifest(run_dir: str | Path) -> dict[str, Any]:
    """读取资源中文分类清单。"""
    manifest_path = Path(run_dir) / MANIFEST_RELATIVE_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("资源清单根节点必须是 JSON object")
    return payload


def _resolve_matches(run_dir: Path, patterns: list[str]) -> list[str]:
    matches: list[str] = []
    for pattern in patterns:
        safe_pattern = _as_relative_posix(pattern)
        for path in sorted(run_dir.glob(safe_pattern)):
            if path.is_file():
                matches.append(path.relative_to(run_dir).as_posix())
    return sorted(dict.fromkeys(matches))


def validate_resource_manifest(run_dir: str | Path) -> dict[str, list[str]]:
    """校验资源清单，并返回每个中文分类匹配到的现有文件。"""
    root = Path(run_dir)
    payload = load_resource_manifest(root)
    if payload.get("schema_version") != 1:
        raise ValueError("资源清单 schema_version 必须为 1")
    if payload.get("phase") not in {"classification-only", "migrated-resources"}:
        raise ValueError("资源清单 phase 必须是 classification-only 或 migrated-resources")

    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("资源清单必须包含非空 categories")

    seen_names: set[str] = set()
    resolved: dict[str, list[str]] = {}
    for item in categories:
        if not isinstance(item, dict):
            raise ValueError("资源分类项必须是 JSON object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("资源分类项缺少 name")
        if name in seen_names:
            raise ValueError(f"资源分类重复：{name}")
        seen_names.add(name)

        resource_kind = str(item.get("resource_kind") or "").strip()
        target_path = _as_relative_posix(str(item.get("target_path_for_phase_2") or ""))
        if not target_path.startswith("resources/"):
            raise ValueError(f"{name} 的后续目标路径必须位于 resources/ 下")

        source_globs = item.get("source_globs")
        if not isinstance(source_globs, list) or not source_globs:
            raise ValueError(f"{name} 缺少 source_globs")
        patterns = [str(pattern) for pattern in source_globs]
        matches = _resolve_matches(root, patterns)
        if not matches and not bool(item.get("allow_empty")):
            raise ValueError(f"{name} 的 source_globs 没有匹配任何现有文件")

        for rel_path in matches:
            if rel_path.startswith("data/runtime/"):
                raise ValueError(f"{name} 不得把运行态文件登记为稳定资源：{rel_path}")
            if rel_path.startswith("data/raw/") and resource_kind not in RAW_ALLOWED_KINDS:
                raise ValueError(f"{name} 登记 data/raw 时必须声明为来源证据或首启快照：{rel_path}")
        resolved[name] = matches
    return resolved


__all__ = ["MANIFEST_RELATIVE_PATH", "load_resource_manifest", "validate_resource_manifest"]
