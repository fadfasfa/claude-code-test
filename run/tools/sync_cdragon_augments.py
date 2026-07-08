"""从 CommunityDragon 刷新 Arena/ARAM 海克斯闭集目录。

本工具只负责把 `cherry-augments.json` 收口成本地稳定资源：
- `data/static/version/海克斯资源目录.v1.json`
- `data/static/assets/*_small.png`

CommunityDragon 的 cherry 数据只含名字、稀有度和图标路径，不含完整 tooltip。
因此本工具不抓取、不保留 description / tooltip；描述仍由第三方数据链路提供。

调用方: 见 import 此模块的代码; 关键依赖: requests、scraping.icon_resolver、catalog.version_catalog。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import requests

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.scraping.icon_resolver import normalize_augment_name, normalize_safe_augment_icon_filename, sanitize_augment_icon_url
from hextech.catalog.version_catalog import get_augment_resource_catalog_path, load_apexlol_slug_map
from hextech.support.atomic_io import atomic_write_json
from hextech.support.image_validation import is_valid_png_bytes


CHERRY_AUGMENTS_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/cherry-augments.json"
)
CDRAGON_ASSET_BASE_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/assets/"
)
MANIFEST_SCHEMA_VERSION = 2
REQUIRED_COVERAGE_NAMES = ("缩小引擎", "坦克引擎", "钢化你心", "尤里卡")
RARITY_TO_TIER = {
    "kBronze": "白银",
    "kSilver": "白银",
    "kGold": "黄金",
    "kPrismatic": "棱彩",
    "kEventChoice": "棱彩",
}

DATA_DIR = RUN_DIR / "data"
STATIC_DIR = DATA_DIR / "static" / "version"
INDEX_DIR = DATA_DIR / "static" / "version"
ASSET_DIR = DATA_DIR / "static" / "assets"
CATALOG_PATH = get_augment_resource_catalog_path(STATIC_DIR)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def build_cdragon_asset_url(icon_path: str) -> str:
    """把 `/lol-game-data/assets/...` 路径转换为 raw.communitydragon.org 可下载 URL。"""

    raw = str(icon_path or "").strip()
    if not raw:
        return ""
    lowered = raw.replace("\\", "/").lstrip("/").lower()
    prefix = "lol-game-data/assets/"
    if lowered.startswith(prefix):
        lowered = lowered[len(prefix) :]
    if lowered.startswith("assets/"):
        lowered = lowered[len("assets/") :]
    if not lowered:
        return ""
    return CDRAGON_ASSET_BASE_URL + quote(lowered, safe="/._-")


def _icon_filename(icon_path: str) -> str:
    relative = str(icon_path or "").replace("\\", "/").rstrip("/")
    return normalize_safe_augment_icon_filename(Path(relative).name)


def fetch_cherry_augments(session: requests.Session, *, timeout: float = 30.0) -> list[dict[str, Any]]:
    response = session.get(CHERRY_AUGMENTS_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("CommunityDragon cherry-augments.json 不是 list")
    return [item for item in payload if isinstance(item, dict)]


def _manifest_entry(raw_item: Mapping[str, Any]) -> dict[str, Any]:
    name = _clean_text(raw_item.get("nameTRA") or raw_item.get("simpleNameTRA"))
    icon_path = _clean_text(raw_item.get("augmentSmallIconPath"))
    filename = _icon_filename(icon_path)
    tier = RARITY_TO_TIER.get(_clean_text(raw_item.get("rarity")), _clean_text(raw_item.get("rarity")))
    icon_url = sanitize_augment_icon_url(f"/assets/{filename}") if filename else build_cdragon_asset_url(icon_path)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "name": name,
        "tier": tier,
        "filename": filename,
        "local_path": f"assets/{filename}" if filename else "",
        "icon_url": icon_url,
        "cdragon_id": raw_item.get("id"),
        "augment_name_id": _clean_text(raw_item.get("augmentNameId")),
        "source_icon_path": icon_path,
        "source_icon_url": build_cdragon_asset_url(icon_path),
        # 显式标记数据层来源，供消费者区分 cdragon 闭集（无描述字段）与 apex 富化 schema，
        # 不再仅靠 source_icon_url 前缀隐式推断。
        "source_schema": "cdragon_minimal",
    }


def _parse_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _canonical_name_rank(entry: Mapping[str, Any]) -> tuple[int, int, int, str]:
    name_id = _clean_text(entry.get("augment_name_id"))
    filename = _clean_text(entry.get("filename"))
    # 同中文名的 legacy 映射优先保留正式 Arena 条目；ARAM / Special / generic 是降级兜底。
    penalty = 0
    if name_id.upper().startswith("ARAM_"):
        penalty += 10
    if name_id.upper().startswith("SPECIAL_"):
        penalty += 5
    if filename.startswith("genericabilityaugmenticon_"):
        penalty += 3
    if name_id.upper().startswith("WEAPON_"):
        penalty += 2
    return (penalty, _parse_int(entry.get("cdragon_id")), len(name_id), name_id)


def _manifest_sort_key(entry: Mapping[str, Any]) -> tuple[str, tuple[int, int, int, str]]:
    # 同名条目按 canonical rank 倒序排，让旧 name-keyed 消费者覆盖时得到稳定 canonical。
    rank = _canonical_name_rank(entry)
    reverse_rank = tuple(-value if isinstance(value, int) else value for value in rank)
    return (normalize_augment_name(entry["name"]), reverse_rank)


def build_manifest(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in raw_items:
        name = _clean_text(item.get("nameTRA") or item.get("simpleNameTRA"))
        stable_key = _clean_text(item.get("augmentNameId")) or str(item.get("id") or "").strip() or name
        if not name or stable_key in seen_keys:
            continue
        entry = _manifest_entry(item)
        if not entry["filename"]:
            entry["status"] = "missing_icon"
        entries.append(entry)
        seen_keys.add(stable_key)
    return sorted(entries, key=_manifest_sort_key)


def _ambiguous_name_groups(manifest: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest:
        by_name.setdefault(_clean_text(entry.get("name")), []).append(entry)

    groups: dict[str, list[dict[str, str]]] = {}
    for name, entries in by_name.items():
        variants = {
            (_clean_text(entry.get("tier")), _clean_text(entry.get("filename")))
            for entry in entries
        }
        if len(variants) <= 1:
            continue
        groups[name] = [
            {
                "tier": _clean_text(entry.get("tier")),
                "filename": _clean_text(entry.get("filename")),
                "augment_name_id": _clean_text(entry.get("augment_name_id")),
                "cdragon_id": str(entry.get("cdragon_id") or ""),
            }
            for entry in sorted(entries, key=_canonical_name_rank)
        ]
    return dict(sorted(groups.items(), key=lambda item: normalize_augment_name(item[0])))


def build_name_to_icon(manifest: list[dict[str, Any]], raw_items: list[dict[str, Any]]) -> dict[str, str]:
    by_name = {entry["name"]: entry for entry in manifest if entry.get("name")}
    mapping: dict[str, str] = {}
    for item in raw_items:
        names = [_clean_text(item.get("nameTRA")), _clean_text(item.get("simpleNameTRA"))]
        primary = names[0] or names[1]
        entry = by_name.get(primary)
        filename = str(entry.get("filename") or "").strip() if entry else ""
        if not filename:
            continue
        for name in names:
            if name:
                mapping[name] = f"/assets/{filename}"
    return dict(sorted(mapping.items(), key=lambda item: normalize_augment_name(item[0])))


def _download_one(
    entry: Mapping[str, Any],
    *,
    force: bool,
    timeout: float,
    asset_dir: Path = ASSET_DIR,
) -> dict[str, Any]:
    filename = str(entry.get("filename") or "").strip()
    url = str(entry.get("source_icon_url") or "").strip()
    if not filename or not url:
        return {"name": entry.get("name"), "filename": filename, "status": "skipped", "reason": "missing_url_or_filename"}
    target = asset_dir / filename
    if target.exists() and target.stat().st_size > 0 and not force:
        return {"name": entry.get("name"), "filename": filename, "status": "cached"}
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    content = response.content
    if not is_valid_png_bytes(content):
        raise ValueError("invalid png icon response")
    asset_dir.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}-", suffix=".tmp", dir=asset_dir)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
    return {"name": entry.get("name"), "filename": filename, "status": "downloaded", "bytes": len(content)}


def download_icons(
    manifest: list[dict[str, Any]],
    *,
    force: bool,
    max_workers: int,
    timeout: float,
    asset_dir: Path = ASSET_DIR,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    unique_entries: dict[str, Mapping[str, Any]] = {}
    for entry in manifest:
        filename = str(entry.get("filename") or "").strip()
        if filename and filename not in unique_entries:
            unique_entries[filename] = entry
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [
            pool.submit(_download_one, entry, force=force, timeout=timeout, asset_dir=asset_dir)
            for entry in unique_entries.values()
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"status": "failed", "reason": str(exc)})
    return results


def sync_cdragon_augments(
    *,
    download: bool,
    force_icons: bool,
    max_workers: int,
    timeout: float,
    asset_dir: Path = ASSET_DIR,
    catalog_path: Path = CATALOG_PATH,
    static_dir: Path = STATIC_DIR,
) -> dict[str, Any]:
    with requests.Session() as session:
        raw_items = fetch_cherry_augments(session, timeout=timeout)
    manifest = build_manifest(raw_items)
    name_to_icon = build_name_to_icon(manifest, raw_items)
    ambiguous_names = _ambiguous_name_groups(manifest)

    icon_results = (
        download_icons(
            manifest,
            force=force_icons,
            max_workers=max_workers,
            timeout=timeout,
            asset_dir=asset_dir,
        )
        if download
        else []
    )
    failed_icons = [item for item in icon_results if item.get("status") == "failed"]
    missing_local_icons = [
        entry["filename"]
        for entry in manifest
        if entry.get("filename") and not (asset_dir / str(entry["filename"])).exists()
    ]
    for entry in manifest:
        if entry.get("filename") in missing_local_icons:
            entry["status"] = "missing_icon"

    static_dir.mkdir(parents=True, exist_ok=True)
    catalog_payload = {
        "schema_version": 1,
        "description": "海克斯名称、tier、图标文件、URL 和 apexlol slug 的统一目录。",
        "entries": manifest,
        "name_to_icon": name_to_icon,
        "apexlol_slug_map": load_apexlol_slug_map(static_dir),
    }
    atomic_write_json(catalog_path, catalog_payload, ensure_ascii=False, indent=2)

    coverage = {}
    by_name = {entry["name"]: entry for entry in manifest}
    for name in REQUIRED_COVERAGE_NAMES:
        entry = by_name.get(name)
        coverage[name] = {
            "found": bool(entry),
            "filename": str(entry.get("filename") or "") if entry else "",
            "tier": str(entry.get("tier") or "") if entry else "",
            "local_icon": bool(entry and entry.get("filename") and (asset_dir / str(entry["filename"])).exists()),
        }

    status_counts: dict[str, int] = {}
    for result in icon_results:
        status = str(result.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "source": CHERRY_AUGMENTS_URL,
        "raw_count": len(raw_items),
        "manifest_count": len(manifest),
        "name_to_icon_count": len(name_to_icon),
        "icon_result_counts": status_counts,
        "failed_icon_count": len(failed_icons),
        "failed_icon_sample": failed_icons[:20],
        "missing_local_icon_count": len(missing_local_icons),
        "missing_local_icon_sample": missing_local_icons[:20],
        "ambiguous_name_count": len(ambiguous_names),
        "ambiguous_name_sample": dict(list(ambiguous_names.items())[:20]),
        "coverage": coverage,
        "catalog_path": str(catalog_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="从 CommunityDragon 刷新海克斯名字、tier 与图标闭集。")
    parser.add_argument("--no-download", action="store_true", help="只写 manifest/index，不下载图标。")
    parser.add_argument("--force-icons", action="store_true", help="重新下载已存在的图标文件。")
    parser.add_argument("--max-workers", type=int, default=8, help="并发下载图标数量。")
    parser.add_argument("--timeout", type=float, default=30.0, help="网络请求超时秒数。")
    args = parser.parse_args()

    summary = sync_cdragon_augments(
        download=not args.no_download,
        force_icons=args.force_icons,
        max_workers=args.max_workers,
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["manifest_count"] >= 600 and all(item["found"] for item in summary["coverage"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
