"""现行 resources/var 分类清单校验。

本模块验证 `resources/manifest.v1.json` 的结构、glob 和打包边界，不移动文件。
稳定资源必须位于 resources，运行态 var 与测试 fixture 必须明确禁止打包。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MANIFEST_RELATIVE_PATH = Path("resources") / "manifest.v1.json"


def _as_relative_posix(path_text: str) -> str:
    path = Path(str(path_text or ""))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"资源清单路径必须是仓库内相对路径：{path_text}")
    return path.as_posix().strip("/")


def load_resource_manifest(run_dir: str | Path) -> dict[str, Any]:
    """读取数据目录分类清单。"""
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
    """校验数据清单，并返回每个分类匹配到的现有文件。"""
    root = Path(run_dir)
    payload = load_resource_manifest(root)
    if payload.get("schema_version") != 1:
        raise ValueError("数据清单 schema_version 必须为 1")
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("资源清单缺少 rules")
    if rules.get("resource_root") != "resources" or rules.get("runtime_root") != "var":
        raise ValueError("资源清单必须声明 resources/var 根边界")
    if rules.get("runtime_must_not_be_bundled") is not True:
        raise ValueError("资源清单必须禁止打包 var")

    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("数据清单必须包含非空 categories")

    seen_names: set[str] = set()
    resolved: dict[str, list[str]] = {}
    for item in categories:
        if not isinstance(item, dict):
            raise ValueError("数据分类项必须是 JSON object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("数据分类项缺少 name")
        if name in seen_names:
            raise ValueError(f"数据分类重复：{name}")
        seen_names.add(name)

        resource_kind = str(item.get("resource_kind") or "").strip()
        category_path = _as_relative_posix(str(item.get("path") or ""))
        if category_path != "var" and not category_path.startswith(("resources/", "tests/", "var/")):
            raise ValueError(f"{name} 的路径不在 resources、tests 或 var：{category_path}")

        source_globs = item.get("source_globs")
        if not isinstance(source_globs, list) or not source_globs:
            raise ValueError(f"{name} 缺少 source_globs")
        patterns = [str(pattern) for pattern in source_globs]
        matches = _resolve_matches(root, patterns)
        package_role = str(item.get("package_role") or "").strip()
        may_be_empty = (
            bool(item.get("allow_empty"))
            or category_path == "var"
            or category_path.startswith("var/")
            or package_role == "forbidden"
        )
        if not matches and not may_be_empty:
            raise ValueError(f"{name} 的 source_globs 没有匹配任何现有文件")

        for rel_path in matches:
            if category_path.startswith("resources/") and not rel_path.startswith("resources/"):
                raise ValueError(f"{name} 的稳定资源 glob 越界：{rel_path}")
        if (category_path == "var" or category_path.startswith(("var/", "tests/"))) and package_role != "forbidden":
            raise ValueError(f"{name} 必须标记为 forbidden：{category_path}")
        resolved[name] = matches
    return resolved


def main() -> int:
    run_dir = Path(__file__).resolve().parents[2]
    resolved = validate_resource_manifest(run_dir)
    print(f"资源清单校验通过：{len(resolved)} 个分类。")
    return 0


__all__ = ["MANIFEST_RELATIVE_PATH", "load_resource_manifest", "validate_resource_manifest"]


if __name__ == "__main__":
    raise SystemExit(main())
