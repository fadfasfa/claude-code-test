"""海克斯图标查找、缓存与远端兜底。

文件职责：
- 统一解析海克斯图标文件名、清单缓存和本地资源路径
- 在本地图标缺失时执行 CommunityDragon / apexlol 远端回退

核心输入：
- `resources/catalog/海克斯资源目录.v1.json`
- 本地 `resources/assets/` 目录

核心输出：
- 本地图标文件名
- 本地图标 URL 或远端兜底 URL
- 批量预取结果

主要依赖：
- 本地 `resources/catalog` 和 `resources/assets`
- CommunityDragon 与 apexlol

维护提醒：
- 资源缓存策略和失败 TTL 要与 Web / UI 热路径一起评估，避免重复下载

调用方: catalog.view_adapter、display.web.runtime、overlay.hints; 关键依赖: requests、catalog.version_catalog、scraping._paths。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests

from hextech.modules.data.catalog.version_catalog import load_apexlol_slug_map, load_augment_manifest_entries
from hextech.modules.data.ports.paths import ASSET_DIR, STATIC_DATA_DIR
from hextech.modules.vision.image_validation import is_valid_png_bytes, read_limited_response_bytes


_ICON_MAP_CACHE: Tuple[str, float, dict] = ("", 0.0, {})
_APEXLOL_MAP_CACHE: Tuple[str, float, dict] = ("", 0.0, {})
_ICON_MAP_LOCK = threading.Lock()
_APEXLOL_MAP_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()
_ICON_FAILURE_CACHE: Dict[str, float] = {}
_ICON_FAILURE_CACHE_LOCK = threading.Lock()
_FAILURE_TTL_SECONDS = 180
_ICON_WRITE_LOCK = threading.Lock()
_SAFE_ICON_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_ALLOWED_REMOTE_ICON_HOSTS = {
    "raw.communitydragon.org",
    "cdn.communitydragon.org",
    "ddragon.leagueoflegends.com",
    "apexlol.info",
}
MAX_APEXLOL_HEXTECH_MAP_BYTES = 5 * 1024 * 1024


def _resolve_config_dir(config_dir: Optional[str]) -> str:
    if config_dir:
        return config_dir
    return STATIC_DATA_DIR


def _resolve_assets_dir(asset_dir: Optional[str]) -> str:
    return os.path.abspath(asset_dir) if asset_dir else ASSET_DIR


def _resolve_assets_dir_for_config(config_dir: Optional[str]) -> str:
    config_path = os.path.abspath(_resolve_config_dir(config_dir))
    if os.path.basename(config_path) != "catalog":
        return _resolve_assets_dir(None)
    return os.path.join(os.path.dirname(config_path), "assets")


def normalize_augment_name(name: str) -> str:
    name = str(name).lower()
    # 用于名称闭集和 overlay hint 关联；这里主动抹平常见中英文标点差异。
    for token in (" ", "\t", "\n", "-", "_", "(", ")", "[", "]", "'", '"', ".", ":", "：", "，", ",", "、", "/", "／"):
        name = name.replace(token, "")
    return name


def normalize_augment_filename(value: str) -> str:
    return os.path.basename(str(value).strip()).lower()


def is_safe_augment_icon_filename(filename: str) -> bool:
    """只允许本地海克斯图标使用简单 png 文件名。"""
    raw_value = str(filename or "").strip()
    normalized = os.path.basename(raw_value)
    return bool(raw_value and normalized == raw_value and _SAFE_ICON_FILENAME_RE.fullmatch(normalized))


def normalize_safe_augment_icon_filename(value: str) -> str:
    filename = normalize_augment_filename(value)
    return filename if is_safe_augment_icon_filename(filename) else ""


def is_safe_local_asset_url(url: str) -> bool:
    """校验 manifest 中的本地图标 URL，拒绝目录穿越和非 png。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if not parsed.path.startswith("/assets/augments/"):
        return False
    filename = unquote(parsed.path.removeprefix("/assets/augments/"))
    return is_safe_augment_icon_filename(filename)


def is_safe_remote_icon_url(url: str) -> bool:
    """校验可用于图标兜底或 redirect 的远端 URL 白名单。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in _ALLOWED_REMOTE_ICON_HOSTS


def sanitize_augment_icon_url(url: str) -> str:
    """把 manifest/远端 metadata 中的图标 URL 收口到安全白名单。"""
    value = str(url or "").strip()
    if not value:
        return ""
    if is_safe_local_asset_url(value):
        filename = unquote(urlparse(value).path.removeprefix("/assets/augments/"))
        return f"/assets/augments/{filename}"
    if is_safe_remote_icon_url(value):
        return value
    return ""


def find_existing_augment_asset_filename(asset_dir: Optional[str], candidate_filename: str) -> Optional[str]:
    """在本地资源目录中查找最匹配的海克斯图标文件名。"""
    asset_dir = _resolve_assets_dir(asset_dir)
    candidate = normalize_safe_augment_icon_filename(candidate_filename)
    if not candidate:
        return None

    direct_path = os.path.join(asset_dir, "augments", candidate)
    if os.path.exists(direct_path):
        return os.path.basename(direct_path)

    candidate_stem = os.path.splitext(candidate)[0]
    normalized_candidate_stem = normalize_augment_name(candidate_stem)
    best_match = None
    try:
        for entry in os.scandir(asset_dir):
            if not entry.is_file():
                continue
            entry_name = entry.name
            entry_norm = normalize_augment_filename(entry_name)
            if entry_norm == candidate:
                return entry_name
            entry_stem = os.path.splitext(entry_name)[0]
            entry_stem_norm = normalize_augment_name(entry_stem)
            if entry_stem_norm == normalized_candidate_stem:
                return entry_name
            if best_match is None and (
                entry_stem_norm.startswith(normalized_candidate_stem)
                or normalized_candidate_stem.startswith(entry_stem_norm)
            ):
                best_match = entry_name
    except OSError:
        return None
    return best_match


def load_augment_icon_map(config_dir: Optional[str] = None, force_refresh: bool = False) -> dict:
    """读取海克斯图标映射并按文件 mtime 做内存缓存。"""
    global _ICON_MAP_CACHE
    config_dir = _resolve_config_dir(config_dir)
    icon_map_path = os.path.join(config_dir, "Augment_Icon_Map.json")
    manifest_path = os.path.join(config_dir, "Augment_Icon_Manifest.json")

    with _ICON_MAP_LOCK:
        cached_path, cached_mtime, cached_data = _ICON_MAP_CACHE
        if not force_refresh and cached_path == icon_map_path and cached_data:
            try:
                if os.path.getmtime(icon_map_path) == cached_mtime:
                    return cached_data
            except OSError:
                return cached_data

    try:
        current_mtime = os.path.getmtime(icon_map_path)
    except OSError:
        return _load_augment_icon_map_from_manifest(manifest_path)

    try:
        with open(icon_map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _ICON_MAP_LOCK:
                _ICON_MAP_CACHE = (icon_map_path, current_mtime, data)
            return data
    except Exception:
        pass

    return _load_augment_icon_map_from_manifest(manifest_path)


def _load_augment_icon_map_from_manifest(manifest_path: str) -> dict:
    manifest = load_augment_manifest_entries(os.path.dirname(manifest_path))
    if not manifest:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return _ICON_MAP_CACHE[2]

    if not isinstance(manifest, list):
        return _ICON_MAP_CACHE[2]

    data = {}
    for item in manifest:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        filename = normalize_safe_augment_icon_filename(item.get("filename", ""))
        if name and filename:
            data[name] = filename
    return data


def find_augment_icon_filename(icon_map: dict, lookup_name: str, asset_dir: Optional[str] = None) -> Optional[str]:
    if not icon_map or not lookup_name:
        return None

    direct = icon_map.get(lookup_name)
    if direct:
        local_filename = find_existing_augment_asset_filename(asset_dir, direct)
        return local_filename or normalize_safe_augment_icon_filename(direct)

    normalized_lookup = normalize_augment_name(lookup_name)
    for key, value in icon_map.items():
        if normalize_augment_name(key) == normalized_lookup:
            local_filename = find_existing_augment_asset_filename(asset_dir, value)
            return local_filename or normalize_safe_augment_icon_filename(value)
    return None


def build_local_augment_icon_url(hextech_name: str, config_dir: Optional[str] = None) -> str:
    """优先返回本地图标 URL；本地无命中时回退 apexlol 远端图标地址。"""
    config_dir = _resolve_config_dir(config_dir)
    request_name = str(hextech_name).strip()
    asset_name = request_name
    asset_dir = _resolve_assets_dir_for_config(config_dir)

    icon_map = load_augment_icon_map(config_dir=config_dir)
    if icon_map:
        mapped_value = icon_map.get(request_name)
        if mapped_value is None:
            normalized_name = normalize_augment_name(request_name)
            for key, value in icon_map.items():
                if normalize_augment_name(key) == normalized_name:
                    mapped_value = value
                    break
        if mapped_value:
            resolved_name = find_existing_augment_asset_filename(
                asset_dir,
                str(mapped_value).split("/")[-1].strip(),
            )
            asset_name = resolved_name or normalize_safe_augment_icon_filename(str(mapped_value).split("/")[-1].strip())
        else:
            apexlol_url = resolve_apexlol_hextech_icon_url(request_name, config_dir=config_dir)
            if apexlol_url:
                return apexlol_url

    if asset_name == request_name:
        apexlol_url = resolve_apexlol_hextech_icon_url(request_name, config_dir=config_dir)
        if apexlol_url:
            return apexlol_url

    raw_asset_name = str(asset_name or "").strip()
    if raw_asset_name and not os.path.splitext(raw_asset_name)[1]:
        raw_asset_name = f"{raw_asset_name}.png"
    asset_name = normalize_safe_augment_icon_filename(raw_asset_name)
    if not asset_name:
        return ""
    return f"/assets/augments/{quote(asset_name, safe='')}"


def _iter_augment_icon_urls(icon_filename: str):
    filename = normalize_safe_augment_icon_filename(icon_filename)
    if not filename:
        return
    templates = [
        "https://raw.communitydragon.org/latest/game/assets/ux/augments/{filename}",
        "https://raw.communitydragon.org/pbe/game/assets/ux/cherry/augments/icons/{filename}",
        "https://raw.communitydragon.org/pbe/game/assets/ux/augments/{filename}",
    ]
    for template in templates:
        yield template.format(filename=filename)


def _get_download_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "download_session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _THREAD_LOCAL.download_session = session
    return session


def _clear_augment_icon_failure(icon_filename: str) -> None:
    with _ICON_FAILURE_CACHE_LOCK:
        _ICON_FAILURE_CACHE.pop(normalize_augment_filename(icon_filename), None)


def _should_skip_failed_icon(icon_filename: str, force_refresh: bool) -> bool:
    if force_refresh:
        return False
    normalized = normalize_augment_filename(icon_filename)
    now = time.time()
    with _ICON_FAILURE_CACHE_LOCK:
        failed_at = _ICON_FAILURE_CACHE.get(normalized)
        if failed_at is None:
            return False
        if (now - failed_at) < _FAILURE_TTL_SECONDS:
            return True
        _ICON_FAILURE_CACHE.pop(normalized, None)
    return False


def _mark_augment_icon_failure(icon_filename: str) -> None:
    with _ICON_FAILURE_CACHE_LOCK:
        _ICON_FAILURE_CACHE[normalize_augment_filename(icon_filename)] = time.time()


def ensure_augment_icon_cached(icon_filename: str, asset_dir: Optional[str] = None, force_refresh: bool = False) -> Optional[str]:
    """确保指定海克斯图标已缓存在本地资源目录，必要时执行远端下载。"""
    asset_dir = _resolve_assets_dir(asset_dir)
    normalized_filename = normalize_safe_augment_icon_filename(icon_filename)
    if not normalized_filename:
        return None

    target_path = os.path.join(asset_dir, normalized_filename)
    if not force_refresh and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        _clear_augment_icon_failure(normalized_filename)
        return target_path
    if _should_skip_failed_icon(normalized_filename, force_refresh):
        return None

    os.makedirs(asset_dir, exist_ok=True)
    for url in _iter_augment_icon_urls(normalized_filename):
        try:
            response = _get_download_session().get(url, stream=True, timeout=15)
            if response.status_code != 200:
                continue
            content = read_limited_response_bytes(response)
            if content is None:
                continue
            if not is_valid_png_bytes(content):
                continue
            with _ICON_WRITE_LOCK:
                fd, tmp_path = tempfile.mkstemp(prefix="augment-cache-", suffix=".tmp", dir=asset_dir)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(content)
                    os.replace(tmp_path, target_path)
                finally:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except OSError:
                        pass
            _clear_augment_icon_failure(normalized_filename)
            return target_path
        except requests.RequestException:
            continue
        except OSError:
            continue

    _mark_augment_icon_failure(normalized_filename)
    return None


def batch_prefetch_augment_icons(
    icon_filenames: Iterable[str],
    asset_dir: Optional[str] = None,
    force_refresh: bool = False,
    max_workers: int = 8,
    stop_event=None,
) -> dict:
    """并发预取一批海克斯图标，并返回成功/失败统计。"""
    asset_dir = _resolve_assets_dir(asset_dir)
    unique_filenames = []
    seen = set()
    for raw_name in icon_filenames:
        normalized = normalize_safe_augment_icon_filename(raw_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_filenames.append(normalized)

    result = {"total": len(unique_filenames), "success": 0, "failed": 0, "failed_files": []}
    if not unique_filenames:
        return result

    workers = max(1, min(max_workers, len(unique_filenames)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_filename = {
            executor.submit(ensure_augment_icon_cached, filename, asset_dir, force_refresh): filename
            for filename in unique_filenames
        }
        for future in as_completed(future_to_filename):
            if stop_event is not None and stop_event.is_set():
                for pending in future_to_filename:
                    pending.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                break

            filename = future_to_filename[future]
            try:
                cached_path = future.result()
                if cached_path and os.path.exists(cached_path) and os.path.getsize(cached_path) > 0:
                    result["success"] += 1
                else:
                    result["failed"] += 1
                    result["failed_files"].append(filename)
            except Exception:
                result["failed"] += 1
                result["failed_files"].append(filename)
    return result


def _normalize_apexlol_hextech_slug(value: str) -> str:
    value = str(value).strip()
    return value.lstrip("/").split("?")[0].split("#")[0]


def _repair_mojibake_text(value: str) -> str:
    """尽量还原把 UTF-8 误当 latin1 写入后的中文 key。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        repaired = text.encode("latin1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired or text


def _expand_apexlol_hextech_map(raw_map: dict) -> dict:
    """补齐中文 key 与 normalized key，兼容历史乱码 map。"""
    expanded: Dict[str, str] = {}
    for raw_key, raw_value in raw_map.items():
        slug = _normalize_apexlol_hextech_slug(raw_value)
        if not slug:
            continue
        key = str(raw_key or "").strip()
        repaired_key = _repair_mojibake_text(key)
        for candidate in (key, repaired_key, normalize_augment_name(key), normalize_augment_name(repaired_key)):
            if candidate:
                expanded.setdefault(candidate, slug)
    return expanded


def load_apexlol_hextech_map(config_dir: Optional[str] = None, force_refresh: bool = False) -> dict:
    """加载或抓取 apexlol 海克斯 slug 映射，供远端图标兜底使用。"""
    global _APEXLOL_MAP_CACHE
    config_dir = _resolve_config_dir(config_dir)
    map_path = os.path.join(config_dir, "Augment_Apexlol_Map.json")

    with _APEXLOL_MAP_LOCK:
        cached_path, cached_mtime, cached_data = _APEXLOL_MAP_CACHE
        if not force_refresh and cached_path == map_path and cached_data:
            try:
                if os.path.getmtime(map_path) == cached_mtime:
                    return cached_data
            except OSError:
                return cached_data

    try:
        current_mtime = os.path.getmtime(map_path)
    except OSError:
        current_mtime = 0.0
        catalog_data = load_apexlol_slug_map(config_dir)
        if catalog_data:
            data = _expand_apexlol_hextech_map(catalog_data)
            with _APEXLOL_MAP_LOCK:
                _APEXLOL_MAP_CACHE = (map_path, current_mtime, data)
            return data

    if not force_refresh and current_mtime and os.path.exists(map_path):
        try:
            with open(map_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                data = _expand_apexlol_hextech_map(data)
                with _APEXLOL_MAP_LOCK:
                    _APEXLOL_MAP_CACHE = (map_path, current_mtime, data)
                return data
        except Exception:
            pass

    response = None
    try:
        response = requests.get(
            "https://apexlol.info/zh/hextech/",
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
            stream=True,
        )
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAX_APEXLOL_HEXTECH_MAP_BYTES:
            return _APEXLOL_MAP_CACHE[2]
        chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > MAX_APEXLOL_HEXTECH_MAP_BYTES:
                response.close()
                return _APEXLOL_MAP_CACHE[2]
            chunks.append(chunk)
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except Exception:
        return _APEXLOL_MAP_CACHE[2]
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass

    name_to_slug: Dict[str, str] = {}
    for match in re.finditer(r'href="/zh/hextech/([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        slug = _normalize_apexlol_hextech_slug(match.group(1))
        inner_html = match.group(2)
        title = re.sub(r"<[^>]+>", " ", inner_html)
        title = unescape(title)
        title = re.sub(r"\s+", " ", title).strip()
        if not slug or not title:
            continue
        name_to_slug.setdefault(title, slug)
        name_to_slug.setdefault(normalize_augment_name(title), slug)
    name_to_slug = _expand_apexlol_hextech_map(name_to_slug)

    if name_to_slug:
        try:
            os.makedirs(config_dir, exist_ok=True)
            tmp_path = map_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(name_to_slug, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, map_path)
            with _APEXLOL_MAP_LOCK:
                _APEXLOL_MAP_CACHE = (map_path, time.time(), name_to_slug)
        except Exception:
            pass
        return name_to_slug

    return _APEXLOL_MAP_CACHE[2]


def resolve_apexlol_hextech_icon_url(hextech_name: str, config_dir: Optional[str] = None) -> str:
    """把海克斯名称解析成 apexlol 图标 URL，作为本地图标缺失时的最终回退。"""
    slug_map = load_apexlol_hextech_map(config_dir=config_dir)
    candidates = [str(hextech_name).strip(), normalize_augment_name(hextech_name)]
    for candidate in candidates:
        slug = slug_map.get(candidate)
        if slug:
            remote_url = f"https://apexlol.info/images/hextech/{slug}.webp"
            return remote_url if is_safe_remote_icon_url(remote_url) else ""
    return ""
