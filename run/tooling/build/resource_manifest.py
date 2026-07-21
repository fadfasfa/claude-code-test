"""生成并校验 ``resources/manifest.v2.json``。

清单逐文件记录路径、角色、大小和 SHA-256。构建阶段只消费清单中的
``package`` 条目；未列入清单的文件只进入审计结果，不会隐式进入发布包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MANIFEST_RELATIVE_PATH = Path("resources") / "manifest.v2.json"
PACKAGE_ROLE = "package"
BUILD_ONLY_ROLE = "build-only"
ALLOWED_ASSET_CATEGORIES = ("augments", "champions", "modes", "ui", "vision")
CATALOG_FILENAMES = (
    "manifest.v2.json",
    "英雄目录.v1.json",
    "海克斯资源目录.v1.json",
    "hero_version.txt",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_relative_posix(path_text: str) -> str:
    path = Path(str(path_text or ""))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"资源清单路径必须是仓库内相对路径：{path_text}")
    normalized = path.as_posix().strip("/")
    if not normalized.startswith("resources/"):
        raise ValueError(f"资源清单文件必须位于 resources：{path_text}")
    return normalized


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return sorted(path for path in root.rglob("*") if path.is_file())


def _seed_files(resource_root: Path) -> list[Path]:
    """只选择 v2 pointer 指向的 immutable seed generation。"""

    seed_root = resource_root / "seeds"
    pointer = seed_root / "current.v2.json"
    if not pointer.is_file():
        return []
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"v2 seed pointer 无法读取：{pointer}") from exc
    generation_id = str(payload.get("current_generation_id") or "") if isinstance(payload, dict) else ""
    generation_root = (seed_root / "generations" / generation_id).resolve()
    expected_parent = (seed_root / "generations").resolve()
    if not generation_id or generation_root.parent != expected_parent or not generation_root.is_dir():
        raise ValueError("v2 seed pointer 未指向有效 generation")
    return [pointer, *_iter_files(generation_root)]


def _descriptor(run_dir: Path, path: Path, *, category: str, package_role: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(run_dir).as_posix(),
        "category": category,
        "package_role": package_role,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def build_resource_manifest(run_dir: str | Path) -> dict[str, Any]:
    """从明确分类目录生成逐文件清单，不扫描 assets 根部平铺文件。"""

    root = Path(run_dir).resolve()
    resource_root = root / "resources"
    descriptors: list[dict[str, Any]] = []

    for filename in CATALOG_FILENAMES:
        path = resource_root / "catalog" / filename
        if path.is_file():
            descriptors.append(_descriptor(root, path, category="catalog", package_role=PACKAGE_ROLE))

    for category in ALLOWED_ASSET_CATEGORIES:
        for path in _iter_files(resource_root / "assets" / category):
            descriptors.append(_descriptor(root, path, category="assets", package_role=PACKAGE_ROLE))

    for path in _seed_files(resource_root):
        descriptors.append(_descriptor(root, path, category="seeds", package_role=PACKAGE_ROLE))

    for path in _iter_files(resource_root / "evidence"):
        descriptors.append(_descriptor(root, path, category="evidence", package_role=BUILD_ONLY_ROLE))

    for path in (resource_root / "README.md", resource_root / "assets" / "README.md"):
        if path.is_file():
            descriptors.append(_descriptor(root, path, category="documentation", package_role=BUILD_ONLY_ROLE))

    descriptors.sort(key=lambda item: str(item["path"]))
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": "Hextech resources 逐文件完整性与发布白名单。",
        "rules": {
            "resource_root": "resources",
            "runtime_root": "var",
            "runtime_must_not_be_bundled": True,
            "unlisted_files_must_not_be_bundled": True,
        },
        "files": descriptors,
    }


def load_resource_manifest(run_dir: str | Path) -> dict[str, Any]:
    manifest_path = Path(run_dir) / MANIFEST_RELATIVE_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("资源清单根节点必须是 JSON object")
    return payload


def validate_resource_manifest(run_dir: str | Path) -> dict[str, Any]:
    """校验清单文件及哈希，并返回打包白名单和未列文件审计。"""

    root = Path(run_dir).resolve()
    payload = load_resource_manifest(root)
    if payload.get("schema_version") != 2:
        raise ValueError("资源清单 schema_version 必须为 2")
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("资源清单缺少 rules")
    if rules.get("resource_root") != "resources" or rules.get("runtime_root") != "var":
        raise ValueError("资源清单必须声明 resources/var 根边界")
    if rules.get("runtime_must_not_be_bundled") is not True:
        raise ValueError("资源清单必须禁止打包 var")
    if rules.get("unlisted_files_must_not_be_bundled") is not True:
        raise ValueError("资源清单必须禁止打包未列文件")

    items = payload.get("files")
    if not isinstance(items, list) or not items:
        raise ValueError("资源清单必须包含非空 files")

    seen: set[str] = set()
    packaged: list[str] = []
    listed: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("资源文件项必须是 JSON object")
        rel_path = _as_relative_posix(str(item.get("path") or ""))
        if rel_path in seen:
            raise ValueError(f"资源文件重复：{rel_path}")
        seen.add(rel_path)
        path = (root / rel_path).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"资源文件不存在或越界：{rel_path}")
        role = str(item.get("package_role") or "")
        if role not in {PACKAGE_ROLE, BUILD_ONLY_ROLE}:
            raise ValueError(f"资源文件 package_role 无效：{rel_path}")
        expected_size = item.get("size")
        if not isinstance(expected_size, int) or expected_size != path.stat().st_size:
            raise ValueError(f"资源文件大小不一致：{rel_path}")
        expected_hash = str(item.get("sha256") or "").lower()
        if len(expected_hash) != 64 or expected_hash != _sha256(path):
            raise ValueError(f"资源文件 SHA-256 不一致：{rel_path}")
        listed.append(rel_path)
        if role == PACKAGE_ROLE:
            packaged.append(rel_path)

    manifest_rel = MANIFEST_RELATIVE_PATH.as_posix()
    existing = {
        path.relative_to(root).as_posix()
        for path in _iter_files(root / "resources")
        if path.relative_to(root).as_posix() != manifest_rel
    }
    return {
        "packaged_files": sorted(packaged),
        "listed_files": sorted(listed),
        "unlisted_files": sorted(existing - set(listed)),
    }


def write_resource_manifest(run_dir: str | Path) -> Path:
    root = Path(run_dir).resolve()
    path = root / MANIFEST_RELATIVE_PATH
    payload = build_resource_manifest(root)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验 resources manifest v2")
    parser.add_argument("--write", action="store_true", help="按明确分类目录重写 manifest.v2.json")
    args = parser.parse_args(argv)
    run_dir = Path(__file__).resolve().parents[2]
    if args.write:
        write_resource_manifest(run_dir)
    report = validate_resource_manifest(run_dir)
    print(
        "资源清单校验通过："
        f"package={len(report['packaged_files'])}, unlisted={len(report['unlisted_files'])}。"
    )
    return 0


__all__ = [
    "MANIFEST_RELATIVE_PATH",
    "build_resource_manifest",
    "load_resource_manifest",
    "validate_resource_manifest",
    "write_resource_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
