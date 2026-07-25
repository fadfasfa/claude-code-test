"""Apex 全量验证、单英雄探测与 CLI 编排。"""
from __future__ import annotations

from hextech.infrastructure.sources.apex.common import (
    ApexPageState,
    ChampionInfo,
    FetchedResource,
    Optional,
    Path,
    RUNTIME_DATA_DIR,
    RedactingTextFormatter,
    SourceHealth,
    SourceRunManifest,
    SYNERGY_REFRESH_META_VERSION,
    SynergyEntry,
    _atomic_write_json,
    _load_existing_synergy_stats,
    _load_json_file,
    _safe_exception_label,
    _validate_publish_size,
    argparse,
    build_champion_lookup,
    build_champion_slug_map,
    build_core_info,
    champion_detail_url,
    classify_apex_page,
    csv,
    datetime,
    get_latest_csv,
    item_outcome,
    load_active_catalog,
    load_apexlol_slug_map,
    load_augment_manifest_entries,
    load_champion_core_data,
    log_task_summary,
    logger,
    logging,
    normalize_augment_name,
    normalize_name,
    normalize_slug,
    os,
    publish_apex_run,
    summarize_synergy_payload,
    time,
    utc_now_iso,
    uuid,
    write_run_diagnostics,
    write_synergy_refresh_meta,
)
from hextech.infrastructure.sources.apex.fetcher import ApexSource
from hextech.infrastructure.sources.apex.extractor import SynergyExtractor
from hextech.infrastructure.sources.apex.writer import SynergyWriter
from hextech.contracts import FailureKind
from hextech.modules.acquisition.apex.parser import ApexPageOutcome

# 该模块是旧调用方的稳定 facade；拆分后仍显式保留这两个公开名称。
__all__ = ["SYNERGY_REFRESH_META_VERSION", "write_synergy_refresh_meta"]


def build_augment_name_map_from_static(catalog_root: Path | None = None) -> dict:
    name_map = {}
    root = catalog_root or load_active_catalog().root

    def add_mapping(raw_key, raw_name):
        key = str(raw_key or "").strip()
        name = str(raw_name or "").strip()
        if not key or not name:
            return
        candidates = {key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)}
        for candidate in candidates:
            if candidate:
                name_map.setdefault(candidate, name)

    for raw_name, raw_slug in load_apexlol_slug_map(root).items():
        add_mapping(raw_slug, raw_name)
        add_mapping(raw_name, raw_name)
    for item in load_augment_manifest_entries(root):
        name = str(item.get("name") or "").strip()
        filename_stem = Path(str(item.get("filename") or "")).stem
        add_mapping(name, name)
        add_mapping(filename_stem, name)

    if name_map:
        return name_map

    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        logger.warning("最新 runtime CSV 不存在，无法构建海克斯名称映射")
        return {}

    try:
        import pandas as pd

        df = pd.read_csv(latest_csv, usecols=["海克斯名称"])
        for _, row in df.iterrows():
            name = str(row.get("海克斯名称") or "").strip()
            if name:
                add_mapping(name, name)
    except Exception as exc:
        logger.warning("读取最新 CSV 构建海克斯名称映射失败：%s", _safe_exception_label(exc))
    return name_map


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


def _find_entries_for_champion(champion: ChampionInfo, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
    keys = [champion.id, champion.slug, champion.en_name, champion.name, champion.title, *champion.aliases]
    for key in keys:
        for candidate in (normalize_slug(key), normalize_name(key)):
            if candidate and candidate in synergy_map:
                return synergy_map[candidate]
    return []


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


def _extract_champion_entries(
    extractor: SynergyExtractor,
    champion: ChampionInfo,
    resource: FetchedResource,
) -> tuple[list[SynergyEntry], dict[str, list[SynergyEntry]]]:
    diagnostic_extract = getattr(extractor, "extract_with_diagnostics", None)
    if callable(diagnostic_extract):
        synergy_map, parse_errors = diagnostic_extract([resource])
        if parse_errors:
            raise ValueError(f"联动解析异常；errors={';'.join(parse_errors)}")
    else:
        # 兼容测试替身及旧扩展实现；旧接口抛出的异常仍按 schema failure 处理。
        synergy_map = extractor.extract([resource])
    entries = _find_entries_for_champion(champion, synergy_map)
    if not entries:
        entries = [entry for values in synergy_map.values() for entry in values]
    return entries, synergy_map


def _extract_and_classify_champion(
    extractor: SynergyExtractor,
    champion: ChampionInfo,
    resource: FetchedResource,
    *,
    expected_slug: str,
) -> tuple[list[SynergyEntry], dict[str, list[SynergyEntry]], ApexPageOutcome]:
    """解析异常是 schema failure，绝不能被页面空态 marker 降级为确认空。"""

    try:
        entries, synergy_map = _extract_champion_entries(extractor, champion, resource)
    except Exception:
        return [], {}, ApexPageOutcome(ApexPageState.FAILED, FailureKind.SCHEMA_CHANGED, "parser_exception")
    page = classify_apex_page(
        resource.text,
        expected_slug=expected_slug,
        entry_count=len(entries),
        status_code=resource.status_code,
    )
    return entries, synergy_map, page


def run_full_validation(
    *,
    max_champions: Optional[int] = None,
    report_dir: Optional[str] = None,
    delay_seconds: Optional[float] = None,
    champion_slugs: Optional[list[str]] = None,
) -> dict:
    """全量 dry-run 验证 ApexLoL 详情页抓取与解析，不写正式 synergy 快照。"""
    started_at = time.time()
    out_dir = Path(report_dir).resolve() if report_dir else _default_full_validate_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = out_dir / "stderr.log"
    file_handler = _new_redacting_report_file_handler(stderr_path)
    logging.getLogger().addHandler(file_handler)

    source: Optional[ApexSource] = None
    per_champion: list[dict] = []
    failures: list[dict] = []
    source_checks: list[dict] = []
    combined_synergy_map: dict[str, list[SynergyEntry]] = {}
    cf_blocked_count = 0
    try:
        source = ApexSource()
        core_info = build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
        champions = list(core_info.values())
        if champion_slugs:
            wanted = {normalize_slug(slug) for slug in champion_slugs if str(slug or "").strip()}
            champions = [
                champion
                for champion in champions
                if normalize_slug(champion.en_name or champion.slug or champion.name) in wanted
                or normalize_slug(champion.slug) in wanted
            ]
        if max_champions and max_champions > 0:
            champions = champions[:max_champions]
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(core_info),
            augment_name_map=build_augment_name_map_from_static(),
        )
        delay = delay_seconds
        if delay is None:
            delay = float(os.getenv("APEX_VALIDATE_DELAY_SECONDS", "0") or "0")
        max_attempts = max(1, int(os.getenv("APEX_VALIDATE_FETCH_ATTEMPTS", "2") or "2"))
        for index, champion in enumerate(champions, start=1):
            detail_url = _champion_detail_url(source, champion)
            resource = None
            for attempt in range(1, max_attempts + 1):
                resource = source.fetch(detail_url, allow_browser=True)
                html_for_attempt = resource.text if resource else ""
                if resource and html_for_attempt and not resource.error and not source._origin_failure_reason(html_for_attempt):
                    break
                if attempt < max_attempts:
                    logger.warning(
                        "Apex 全量验证重试：champion=%s attempt=%s error=%s",
                        champion.en_name or champion.slug,
                        attempt,
                        (resource.error if resource else source.last_fetch_error)
                        or source._origin_failure_reason(html_for_attempt)
                        or "empty_html",
                    )
                    time.sleep(max(3.0, delay * attempt))
            html = resource.text if resource else ""
            cf_blocked = source._is_cloudflare_block(html)
            entry_count = 0
            status = "failed"
            error = (resource.error if resource else source.last_fetch_error) or ""
            entries: list[SynergyEntry] = []
            synergy_map: dict[str, list[SynergyEntry]] = {}
            origin_error = source._origin_failure_reason(html)
            if origin_error:
                status = "failed"
                error = error or origin_error
                if origin_error in {"cloudflare_block", "access_denied"}:
                    cf_blocked = True
            elif resource and html and not cf_blocked and not error:
                entries, synergy_map, page = _extract_and_classify_champion(
                    extractor,
                    champion,
                    resource,
                    expected_slug=champion.en_name or champion.slug,
                )
                entry_count = len(entries)
                if page.state is ApexPageState.HAS_SYNERGY:
                    status = "success"
                    for key, values in synergy_map.items():
                        combined_synergy_map.setdefault(key, []).extend(values)
                    if entries and champion.slug != "vi" and len(source_checks) < 3:
                        source_checks.append(_source_check_record(champion, entries[0], html))
                elif page.state is ApexPageState.CONFIRMED_EMPTY:
                    status = "confirmed_empty"
                else:
                    status = "failed"
                    error = page.failure_kind.value if page.failure_kind else "schema_changed"
            elif cf_blocked:
                error = error or "cloudflare_block"
            elif not html:
                error = error or "empty_html"

            if cf_blocked:
                cf_blocked_count += 1

            row = {
                "champion_id": champion.id,
                "champion_slug": champion.slug,
                "champion_name": champion.name,
                "url": detail_url,
                "backend": resource.source if resource else "none",
                "status_code": resource.status_code if resource else None,
                "entry_count": entry_count,
                "status": status,
                "cf_blocked": cf_blocked,
                "error": error,
                "origin_check": "ok" if not origin_error else origin_error,
            }
            per_champion.append(row)
            if status != "success":
                failures.append(row)
            logger.info(
                "Apex 全量验证进度：%s/%s champion=%s status=%s entries=%s cf=%s",
                index,
                len(champions),
                champion.en_name or champion.slug,
                status,
                entry_count,
                cf_blocked,
            )
            if delay > 0 and index < len(champions):
                time.sleep(delay)

        unique_synergy_map = SynergyExtractor._dedupe_entries(combined_synergy_map)
        payload = SynergyWriter(core_info).build_payload(unique_synergy_map)
        stats = summarize_synergy_payload(payload)
        old_stats = _load_existing_synergy_stats()
        publishable = True
        publish_error = ""
        try:
            _validate_publish_size(stats, old_stats)
        except ValueError as exc:
            publishable = False
            publish_error = str(exc)
        failed_count = sum(1 for row in per_champion if row["status"] == "failed")
        if failed_count:
            publishable = False
            failed_error = f"存在未通过 origin 校验的英雄：failed={failed_count}"
            publish_error = f"{publish_error}；{failed_error}" if publish_error else failed_error
        elapsed = round(time.time() - started_at, 3)
        summary = {
            "total_champions": len(core_info),
            "processed": len(per_champion),
            "non_empty": sum(1 for row in per_champion if row["status"] == "success"),
            "empty": sum(1 for row in per_champion if row["status"] == "empty"),
            "real_empty": sum(1 for row in per_champion if row["status"] == "empty"),
            "failed": failed_count,
            "failed_blocked": failed_count,
            "synergy_entries_total": sum(int(row["entry_count"] or 0) for row in per_champion),
            "cf_blocked_count": cf_blocked_count,
            "elapsed": elapsed,
            "stats": stats,
            "publishable": publishable,
            "publish_error": publish_error,
            "dry_run": True,
            "archived_filtered_count": extractor.archived_filtered_count,
            "archived_filter_samples": extractor.archived_filter_samples,
        }
        _atomic_write_json(out_dir / "summary.json", summary)
        _atomic_write_json(out_dir / "per_champion.json", {"items": per_champion})
        _write_per_champion_csv(out_dir / "per_champion.csv", per_champion)
        _atomic_write_json(out_dir / "failures.json", {"items": failures})
        _atomic_write_json(out_dir / "source_checks.json", {"items": source_checks})
        print(
            "full_validate processed={processed} non_empty={non_empty} empty={empty} failed={failed} "
            "synergy_entries_total={entries} cf_blocked_count={cf} publishable={publishable} out_dir={out_dir}".format(
                processed=summary["processed"],
                non_empty=summary["non_empty"],
                empty=summary["empty"],
                failed=summary["failed"],
                entries=summary["synergy_entries_total"],
                cf=summary["cf_blocked_count"],
                publishable=str(summary["publishable"]).lower(),
                out_dir=out_dir,
            )
        )
        return {"out_dir": str(out_dir), **summary}
    finally:
        if source is not None:
            source.close()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def run_single_champion_probe(champion_slug: str = "Vi", report_dir: Optional[str] = None) -> dict:
    """只抓单个 ApexLoL 英雄详情页，并把抓取和解析证据写入 runtime reports。"""
    started_at = time.time()
    out_dir = Path(report_dir).resolve() if report_dir else _default_single_champion_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = out_dir / "stderr.log"
    file_handler = _new_redacting_report_file_handler(stderr_path)
    logging.getLogger().addHandler(file_handler)

    source: Optional[ApexSource] = None
    resource: Optional[FetchedResource] = None
    result = {
        "url": "",
        "backend": "scrapling-get",
        "status_code": None,
        "cf_blocked": True,
        "synergy_entry_count": 0,
        "archived_filtered_count": 0,
        "archived_filter_samples": [],
        "page_state": "failed",
        "page_evidence": "",
        "page_identity_verified": False,
        "error": "",
    }
    try:
        source = ApexSource()
        detail_url = source.build_allowed_url(f"/zh/champions/{champion_slug}")
        if not detail_url:
            raise ValueError(f"英雄 URL 不在 Apex 白名单内：{champion_slug}")
        result["url"] = detail_url
        resource = source.fetch(detail_url, allow_browser=True)
        html = resource.text if resource else ""
        cf_blocked = source._is_cloudflare_block(html)
        result.update(
            {
                "backend": resource.source if resource else "none",
                "status_code": resource.status_code if resource else None,
                "cf_blocked": cf_blocked,
                "error": (resource.error if resource else source.last_fetch_error) or "",
            }
        )
        _write_html_report_sample(out_dir / "page.html.txt", html)

        entries: list[SynergyEntry] = []
        origin_error = source._origin_failure_reason(html)
        if origin_error and not result["error"]:
            result["error"] = origin_error
            result["cf_blocked"] = origin_error in {"cloudflare_block", "access_denied"}
        if resource and html and not result["cf_blocked"] and not result["error"]:
            core_info = _build_single_champion_core_info(champion_slug)
            extractor = SynergyExtractor(
                champion_lookup=build_champion_lookup(core_info),
                augment_name_map=build_augment_name_map_from_static(),
            )
            synergy_map, parse_errors = extractor.extract_with_diagnostics([resource])
            if parse_errors:
                result["error"] = f"schema_changed:{';'.join(parse_errors)}"
            for candidate in (normalize_slug(champion_slug), normalize_name(champion_slug)):
                if candidate and candidate in synergy_map:
                    entries = synergy_map[candidate]
                    break
            if not entries:
                entries = [entry for values in synergy_map.values() for entry in values]
            result["synergy_entry_count"] = len(entries)
            result["archived_filtered_count"] = extractor.archived_filtered_count
            result["archived_filter_samples"] = extractor.archived_filter_samples
            page = classify_apex_page(
                html,
                expected_slug=champion_slug,
                entry_count=len(entries),
                status_code=resource.status_code,
            )
            result["page_state"] = page.state.value
            result["page_evidence"] = page.evidence
            result["page_identity_verified"] = page.state is not ApexPageState.FAILED
            if page.state is ApexPageState.FAILED and not result["error"]:
                result["error"] = page.failure_kind.value if page.failure_kind else "schema_changed"

        synergy_payload = [_entry_to_report_item(entry) for entry in entries]
        _atomic_write_json(out_dir / "synergy_vi.json", synergy_payload)
        if (
            result["synergy_entry_count"] == 0
            and result["page_state"] != ApexPageState.CONFIRMED_EMPTY.value
            and not result["error"]
        ):
            if result["cf_blocked"]:
                result["error"] = "cloudflare_block"
            elif not html:
                result["error"] = "empty_html"
            else:
                result["error"] = "synergy_parse_empty"
        _atomic_write_json(out_dir / "result.json", result)
        print(
            "backend={backend} cf_blocked={cf_blocked} synergy_entry_count={count} out_dir={out_dir}".format(
                backend=result["backend"],
                cf_blocked=str(result["cf_blocked"]).lower(),
                count=result["synergy_entry_count"],
                out_dir=out_dir,
            )
        )
        log_task_summary(
            logger,
            task="ApexLoL Vi 单英雄抓取验证",
            started_at=started_at,
            success=bool(result["synergy_entry_count"]),
            detail=f"backend={result['backend']} cf_blocked={result['cf_blocked']} items={result['synergy_entry_count']} out_dir={out_dir}",
        )
        return {"out_dir": str(out_dir), **result}
    except Exception as exc:
        if source is None:
            raise
        result["error"] = str(exc)
        if resource is not None:
            result["status_code"] = resource.status_code
        _atomic_write_json(out_dir / "synergy_vi.json", [])
        _atomic_write_json(out_dir / "result.json", result)
        print(
            "backend={backend} cf_blocked={cf_blocked} synergy_entry_count=0 out_dir={out_dir}".format(
                backend=result["backend"],
                cf_blocked=str(result["cf_blocked"]).lower(),
                out_dir=out_dir,
            )
        )
        logger.exception("ApexLoL Vi 单英雄抓取验证失败")
        return {"out_dir": str(out_dir), **result}
    finally:
        if source is not None:
            source.close()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def main(
    *,
    dry_run: Optional[bool] = None,
    output_path: Optional[str] = None,
    promote_current: bool = False,
    pointer_output: str | os.PathLike[str] | None = None,
):
    started_monotonic = time.time()
    started_at = utc_now_iso()
    run_id = f"apex-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    dry_run = (os.getenv("APEX_DRY_RUN", "0").strip() == "1") if dry_run is None else bool(dry_run)
    source: ApexSource | None = None
    outcomes = []
    logger.info("ApexLoL 逐英雄直达抓取开始：run_id=%s dry_run=%s", run_id, dry_run)

    try:
        catalog = load_active_catalog()
        core_data = load_champion_core_data(catalog.root)
        core_info = build_core_info(core_data)
        slug_map = build_champion_slug_map(core_data)
        source = ApexSource()
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(core_info),
            augment_name_map=build_augment_name_map_from_static(catalog.root),
        )
        combined: dict[str, list[SynergyEntry]] = {}
        delay = max(0.0, float(os.getenv("APEX_ONLINE_FETCH_DELAY_SECONDS", "0") or "0"))
        ordered_ids = sorted(slug_map, key=lambda value: int(value) if value.isdigit() else value)

        def fetch_champion(champion_id: str):
            champion = core_info[champion_id]
            url = champion_detail_url(source.base_url, slug_map[champion_id])
            resource = source.fetch(url, allow_browser=True)
            entries: list[SynergyEntry] = []
            extracted: dict[str, list[SynergyEntry]] = {}
            page: ApexPageOutcome | None = None
            if resource and resource.text and not resource.error:
                entries, extracted, page = _extract_and_classify_champion(
                    extractor,
                    champion,
                    resource,
                    expected_slug=slug_map[champion_id],
                )
            if page is None:
                page = classify_apex_page(
                    resource.text if resource else "",
                    expected_slug=slug_map[champion_id],
                    entry_count=len(entries),
                    status_code=resource.status_code if resource else None,
                )
            outcome = item_outcome(
                champion_id,
                page,
                record_count=len(entries),
                backend=resource.source if resource else "none",
                status_code=resource.status_code if resource else None,
                url=url,
            )
            return outcome, extracted, page

        outcomes_by_id = {}
        for index, champion_id in enumerate(ordered_ids):
            outcome, extracted, page = fetch_champion(champion_id)
            outcomes_by_id[champion_id] = outcome
            if page.state is ApexPageState.HAS_SYNERGY:
                for key, values in extracted.items():
                    combined.setdefault(key, []).extend(values)
            if delay and index + 1 < len(ordered_ids):
                time.sleep(delay)

        failed_ids = [champion_id for champion_id in ordered_ids if outcomes_by_id[champion_id].state == "failed"]
        if failed_ids:
            logger.warning("Apex 首轮失败英雄进入低频尾部重试：count=%s", len(failed_ids))
            retry_delay = max(0.5, delay)
            for index, champion_id in enumerate(failed_ids):
                outcome, extracted, page = fetch_champion(champion_id)
                outcomes_by_id[champion_id] = outcome
                if page.state is ApexPageState.HAS_SYNERGY:
                    for key, values in extracted.items():
                        combined.setdefault(key, []).extend(values)
                if index + 1 < len(failed_ids):
                    time.sleep(retry_delay)

        outcomes = [outcomes_by_id[champion_id] for champion_id in ordered_ids]

        combined = SynergyExtractor._dedupe_entries(combined)
        payload = SynergyWriter(core_info).build_payload(combined)
        stats = summarize_synergy_payload(payload)
        failed = [outcome for outcome in outcomes if outcome.state == "failed"]
        min_non_empty = max(1, int(os.getenv("APEX_MIN_NON_EMPTY_HEROES", "1") or "1"))
        publishable = not failed and stats["non_empty_heroes"] >= min_non_empty and stats["synergy_entries"] > 0
        target_path = Path(output_path) if output_path else None

        if output_path:
            SynergyWriter(core_info).write(target_path, payload)
        elif not dry_run and publishable:
            published_path, _ = publish_apex_run(
                payload,
                run_id=run_id,
                outcomes=tuple(outcomes),
                record_count=stats["synergy_entries"],
                started_at=started_at,
                promote_current=promote_current,
                pointer_output=pointer_output,
            )
            target_path = Path(published_path)

        if not publishable:
            manifest = SourceRunManifest(
                source="apex",
                run_id=run_id,
                catalog_generation_id=catalog.generation_id,
                catalog_sha256=catalog.content_sha256,
                health=SourceHealth.FAILED,
                started_at=started_at,
                completed_at=utc_now_iso(),
                expected_items=len(slug_map),
                successful_items=sum(outcome.state == "success" for outcome in outcomes),
                confirmed_empty_items=sum(outcome.state == "confirmed_empty" for outcome in outcomes),
                failed_items=len(failed),
                artifact=None,
                outcomes=tuple(outcomes),
                metadata={"minimum_non_empty_heroes": min_non_empty},
            )
            write_run_diagnostics(
                manifest,
                report={"failure_samples": [outcome.to_dict() for outcome in failed[:20]]},
            )

        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_monotonic,
            success=publishable,
            detail=(
                f"heroes={len(slug_map)} non_empty={stats['non_empty_heroes']} "
                f"confirmed_empty={sum(outcome.state == 'confirmed_empty' for outcome in outcomes)} "
                f"failed={len(failed)} items={stats['synergy_entries']}"
            ),
        )
        return {
            "run_id": run_id,
            "synergy_data": payload,
            "dry_run": dry_run,
            "published": bool(not dry_run and publishable and output_path is None),
            "publishable": publishable,
            "output_path": str(target_path or ""),
            "stats": stats,
            "outcomes": [outcome.to_dict() for outcome in outcomes],
            "archived_filtered_count": extractor.archived_filtered_count,
            "archived_filter_samples": extractor.archived_filter_samples,
        }
    except Exception as exc:
        logger.warning("ApexLoL 来源 run 失败，current 保持不变：%s", exc)
        return {"run_id": run_id, "dry_run": dry_run, "published": False, "error": str(exc)}
    finally:
        if source is not None:
            source.close()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ApexLoL 协同数据抓取器")
    parser.add_argument(
        "--single-champion",
        metavar="SLUG",
        help="只抓单个 ApexLoL 英雄详情页并写入 runtime reports，不发布正式 synergy 快照",
    )
    parser.add_argument(
        "--report-dir",
        help="单英雄验证产物目录；未指定时写入 var/reports/synergy_single_probe/<timestamp>/",
    )
    parser.add_argument(
        "--validate-full",
        action="store_true",
        help="全量 dry-run 验证 172 英雄详情页抓取和解析，并写入 runtime reports，不发布正式快照",
    )
    parser.add_argument(
        "--max-champions",
        type=int,
        default=0,
        help="配合 --validate-full 使用；只验证前 N 个英雄，用于小批量 smoke",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=None,
        help="配合 --validate-full 使用；英雄之间等待秒数，默认读取 APEX_VALIDATE_DELAY_SECONDS 或 0",
    )
    parser.add_argument(
        "--champions",
        help="配合 --validate-full 使用；逗号分隔的英雄英文 slug 清单，用于定向复核",
    )
    parser.add_argument(
        "--allow-online-fetch",
        action="store_true",
        help="显式允许没有本地 snapshot 时访问 ApexLoL 在线页面；也可用 APEX_ALLOW_ONLINE_FETCH=1。",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.allow_online_fetch:
        os.environ["APEX_ALLOW_ONLINE_FETCH"] = "1"
    if args.validate_full:
        run_full_validation(
            max_champions=args.max_champions or None,
            report_dir=args.report_dir,
            delay_seconds=args.delay_seconds,
            champion_slugs=[item.strip() for item in (args.champions or "").split(",") if item.strip()] or None,
        )
    elif args.single_champion:
        run_single_champion_probe(args.single_champion, report_dir=args.report_dir)
    else:
        main()
