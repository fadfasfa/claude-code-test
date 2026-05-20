"""ApexLoL 手动 snapshot 采集器。

本脚本只保存人工低频访问时浏览器正常加载到的同源 HTML/JSON/JS/text
响应，供 `run/scraping/full_synergy_scraper.py` 离线解析。它不使用代理、
stealth、验证码处理或 Cloudflare challenge 处理，也不复用浏览器持久状态。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "run" / "data" / "runtime" / "cache" / "apex_snapshot" / "manual"
DEFAULT_URLS = ("https://apexlol.info/zh", "https://apexlol.info/zh/hextech")
ALLOWED_HOST = "apexlol.info"
ALLOWED_CONTENT_MARKERS = ("html", "json", "javascript", "text")
MIN_PAGE_PAUSE_SECONDS = 10
GOTO_TIMEOUT_MS = 30_000


@dataclass
class ManifestRecord:
    entry_url: str
    response_url: str
    filename: str
    content_type: str
    status: int
    captured_at: str


@dataclass
class PageVisitRecord:
    entry_url: str
    filename: str
    captured_at: str
    ok: bool
    error: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return cleaned or "apexlol"


def suffix_for_content_type(content_type: str) -> Optional[str]:
    lowered = content_type.lower()
    if not any(marker in lowered for marker in ALLOWED_CONTENT_MARKERS):
        return None
    if "json" in lowered:
        return ".json"
    if "javascript" in lowered or "ecmascript" in lowered:
        return ".js"
    if "html" in lowered:
        return ".html"
    return ".txt"


def is_allowed_response_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == ALLOWED_HOST


def build_response_filename(url: str, content_type: str) -> str:
    suffix = suffix_for_content_type(content_type) or ".txt"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{safe_name(url)[:150]}-{digest}{suffix}"


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def capture_snapshots(snapshot_dir: Path, urls: tuple[str, ...] = DEFAULT_URLS) -> dict:
    snapshot_dir = snapshot_dir.resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    response_records: list[ManifestRecord] = []
    page_records: list[PageVisitRecord] = []
    current_entry = {"url": ""}

    with sync_playwright() as playwright:
        # 使用非持久上下文，避免保存或复用 cookies/localStorage 等浏览器状态。
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()

        def on_response(response) -> None:
            response_url = response.url
            content_type = response.headers.get("content-type", "")
            if not is_allowed_response_url(response_url):
                return
            suffix = suffix_for_content_type(content_type)
            if suffix is None:
                return

            try:
                body = response.text()
            except PlaywrightError:
                return
            if not body.strip():
                return

            filename = build_response_filename(response_url, content_type)
            save_text(snapshot_dir / filename, body)
            response_records.append(
                ManifestRecord(
                    entry_url=current_entry["url"],
                    response_url=response_url,
                    filename=filename,
                    content_type=content_type,
                    status=response.status,
                    captured_at=utc_now(),
                )
            )

        page.on("response", on_response)

        for index, url in enumerate(urls):
            current_entry["url"] = url
            filename = f"{safe_name(url)}.html"
            ok = True
            error = ""
            try:
                page.goto(url, wait_until="networkidle", timeout=GOTO_TIMEOUT_MS)
                save_text(snapshot_dir / filename, page.content())
            except PlaywrightTimeoutError as exc:
                ok = False
                error = exc.__class__.__name__
                try:
                    save_text(snapshot_dir / filename, page.content())
                except PlaywrightError:
                    pass
            except PlaywrightError as exc:
                ok = False
                error = exc.__class__.__name__

            page_records.append(
                PageVisitRecord(
                    entry_url=url,
                    filename=filename,
                    captured_at=utc_now(),
                    ok=ok,
                    error=error,
                )
            )

            if index < len(urls) - 1:
                time.sleep(MIN_PAGE_PAUSE_SECONDS)

        context.close()
        browser.close()

    manifest = {
        "generated_at": utc_now(),
        "snapshot_dir": str(snapshot_dir),
        "urls": list(urls),
        "pages": [asdict(record) for record in page_records],
        "responses": [asdict(record) for record in response_records],
    }
    save_text(snapshot_dir / "capture_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低频手动保存 ApexLoL 页面和同源响应 snapshot。")
    parser.add_argument(
        "--snapshot-dir",
        default=str(DEFAULT_SNAPSHOT_DIR),
        help="snapshot 输出目录，默认写入 run/data/runtime/cache/apex_snapshot/manual。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = capture_snapshots(Path(args.snapshot_dir))
    print(
        "Apex snapshot capture complete: "
        f"pages={len(manifest['pages'])} responses={len(manifest['responses'])} "
        f"dir={manifest['snapshot_dir']}"
    )


if __name__ == "__main__":
    main()
