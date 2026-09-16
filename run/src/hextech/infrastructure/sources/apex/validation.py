"""Apex validation/probe-only report helpers; no production fetch or publish."""
from __future__ import annotations

from hextech.infrastructure.sources.apex.common import (
    ChampionInfo, Path, RUNTIME_DATA_DIR, RedactingTextFormatter, SynergyEntry,
    _load_json_file, build_core_info, csv, datetime, logging, normalize_slug,
)
from hextech.infrastructure.sources.apex.fetcher import ApexSource


def _entry_to_report_item(entry: SynergyEntry) -> dict:
    return {
        "champion_slug": entry.champion_slug,
        "augment_names": entry.augment_names,
        "tier": entry.tier,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "is_original": entry.is_original,
        "content": entry.content,
        "upvotes": entry.upvotes,
        "downvotes": entry.downvotes,
    }


def _default_single_champion_report_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(RUNTIME_DATA_DIR) / "reports" / "synergy_single_probe" / timestamp


def _default_full_validate_report_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(RUNTIME_DATA_DIR) / "reports" / "synergy_full_validate" / timestamp


def _write_html_report_sample(output_path: Path, html: str, limit_bytes: int = 200 * 1024) -> None:
    encoded = (html or "").encode("utf-8")[:limit_bytes]
    output_path.write_bytes(encoded.decode("utf-8", errors="ignore").encode("utf-8"))


def _new_redacting_report_file_handler(path: Path) -> logging.FileHandler:
    """为临时 Apex 报表日志创建统一脱敏 handler，避免绕过运行态日志边界。"""

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(RedactingTextFormatter("%(asctime)s - %(levelname)s - %(message)s"))
    return file_handler


def _build_single_champion_core_info(champion_slug: str) -> dict[str, ChampionInfo]:
    try:
        return build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
    except FileNotFoundError:
        # 单英雄 smoke 不能为了补静态资料触发稳定资源同步；缺文件时只补当前英雄的解析锚点。
        slug = str(champion_slug or "").strip()
        return {
            slug: ChampionInfo(
                id=slug,
                name=slug,
                title=slug,
                en_name=slug,
                aliases=[slug],
                slug=normalize_slug(slug),
            )
        }


def _champion_detail_url(source: ApexSource, champion: ChampionInfo) -> str:
    slug = champion.en_name or champion.slug or champion.name or champion.id
    detail_url = source.build_allowed_url(f"/zh/champions/{slug}")
    if not detail_url:
        raise ValueError(f"英雄 URL 不在 Apex 白名单内：{slug}")
    return detail_url


def _source_check_record(champion: ChampionInfo, entry: SynergyEntry, html: str) -> dict:
    content_prefix = entry.content[: min(16, len(entry.content))]
    first_augment = entry.augment_names[0] if entry.augment_names else ""
    return {
        "champion_id": champion.id,
        "champion_slug": champion.slug,
        "champion_name": champion.name,
        "url_slug": champion.en_name,
        "augment": first_augment,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "content_prefix": content_prefix,
        "augment_in_html": bool(first_augment and first_augment in html),
        "author_in_html": bool(entry.author and entry.author in html),
        "content_prefix_in_html": bool(content_prefix and content_prefix in html),
    }


def _write_per_champion_csv(output_path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "champion_id",
        "champion_slug",
        "champion_name",
        "url",
        "backend",
        "status_code",
        "entry_count",
        "status",
        "cf_blocked",
        "error",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
