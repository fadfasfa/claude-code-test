"""Persistent conditional-GET cache for deterministic public source requests.

The request identity binds the URL and normalized query parameters.  Response
validators are never shared across identities.  A 304 is usable only while the
cached body still passes its recorded size and SHA-256 checks.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Mapping

from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
from hextech.modules.data.ports.atomic import atomic_write_json


class ConditionalCacheIntegrityError(RuntimeError):
    """Stored validators or body cannot be trusted."""


class ConditionalCacheBudgetExceeded(ConditionalCacheIntegrityError):
    """A response or source cache would exceed its fixed write budget."""


DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024**2
DEFAULT_MAX_SOURCE_BYTES = 6 * 1024**3
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONDITIONAL_HEADERS = {"if-none-match", "if-modified-since"}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(path: Path, *, parents: bool = True) -> None:
    """Reject every existing reparse/link component without resolving through it."""

    for candidate in reversed((path, *path.parents) if parents else (path,)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ConditionalCacheIntegrityError(f"reparse/link path refused: {candidate}")
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise ConditionalCacheIntegrityError(f"non-directory ancestor: {candidate}")


def _canonical_request_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key).strip().casefold()
        if name and name not in _CONDITIONAL_HEADERS:
            normalized[name] = str(value).strip()
    return dict(sorted(normalized.items()))


def _request_headers_sha256(headers: Mapping[str, str] | None) -> str:
    normalized = _canonical_request_headers(headers)
    if not normalized:
        return ""
    return _sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _canonical_params(params: Mapping[str, object] | None) -> dict[str, object]:
    if not params:
        return {}
    normalized: dict[str, object] = {}
    for key, value in sorted(params.items(), key=lambda item: str(item[0])):
        name = str(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            normalized[name] = value
        elif isinstance(value, (list, tuple)):
            normalized[name] = [str(item) for item in value]
        else:
            normalized[name] = str(value)
    return normalized


def request_identity(
    url: str,
    params: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    payload = {
        "url": str(url),
        "params": _canonical_params(params),
    }
    header_fingerprint = _request_headers_sha256(headers)
    if header_fingerprint:
        payload["request_headers_sha256"] = header_fingerprint
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(encoded)


def _headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    try:
        items = value.items()  # type: ignore[attr-defined]
    except AttributeError:
        return {}
    return {str(key): str(item) for key, item in items}


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return str(value).strip()
    return ""


def parse_retry_after_seconds(
    headers: Mapping[str, str] | None,
    *,
    now: datetime | None = None,
    maximum_seconds: int | None = None,
) -> int | None:
    """Parse Retry-After delta/date and clamp it to the caller's safe bound."""

    if maximum_seconds is not None and (
        isinstance(maximum_seconds, bool)
        or not isinstance(maximum_seconds, int)
        or maximum_seconds < 0
    ):
        raise ValueError("maximum_seconds must be a nonnegative integer")
    value = _header(_headers(headers), "retry-after")
    if not value:
        return None
    try:
        seconds = int(value, 10)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        seconds = math.ceil((retry_at - current).total_seconds())
    if seconds < 0:
        return 0
    return seconds if maximum_seconds is None else min(seconds, maximum_seconds)


def _body(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b""


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class CachedConditionalResponse:
    request_key: str
    body: bytes
    etag: str
    last_modified: str
    stored_at: str


@dataclass(frozen=True)
class ConditionalFetchResult:
    url: str
    body: bytes
    status_code: int | None
    response_headers: dict[str, str]
    fetched_at: str
    error: str = ""
    error_kind: str = ""
    attempts: int = 1
    elapsed_ms: int = 0
    backend: str = "static_http"
    fallback_used: bool = False
    fallback_from: str = ""
    request_key: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="strict")


class ConditionalResponseCache:
    """Content-addressed bodies with an atomic latest-validator manifest."""

    def __init__(
        self,
        root: Path,
        *,
        source: str,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    ) -> None:
        if not source:
            raise ValueError("source must be nonempty")
        for name, value in (
            ("max_response_bytes", max_response_bytes),
            ("max_source_bytes", max_source_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        base = Path(os.path.abspath(root))
        _safe_path(base)
        self.directory = base / _sha256(source.encode("utf-8"))
        self.max_response_bytes = max_response_bytes
        self.max_source_bytes = max_source_bytes

    def _request_dir(self, key: str) -> Path:
        return self.directory / key

    @contextmanager
    def _path_locked(self, directory: Path, lock_name: str = ".lock"):
        _safe_path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        _safe_path(directory)
        lock_path = directory / lock_name
        _safe_path(lock_path)
        lock = InterProcessFileLock(lock_path)
        deadline = time.monotonic() + 30
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError("Conditional response cache lock timed out")
            time.sleep(0.01)
        try:
            _safe_path(directory)
            _safe_path(lock_path)
            yield directory
        finally:
            lock.release()

    @contextmanager
    def _locked(self, key: str):
        with self._path_locked(self._request_dir(key)) as directory:
            yield directory

    @contextmanager
    def _budget_locked(self):
        with self._path_locked(self.directory, ".budget.lock"):
            yield

    def _source_usage(self) -> int:
        _safe_path(self.directory)
        if not self.directory.exists():
            return 0
        total = 0
        for request_dir in self.directory.iterdir():
            _safe_path(request_dir, parents=False)
            if request_dir.name == ".budget.lock" and request_dir.is_file():
                continue
            if _HEX_SHA256.fullmatch(request_dir.name) is None or not request_dir.is_dir():
                raise ConditionalCacheIntegrityError("unknown conditional source cache entry")
            for child in request_dir.iterdir():
                _safe_path(child, parents=False)
                if child.name in {".lock", "response.v1.json"} and child.is_file():
                    continue
                if not child.is_file() or re.fullmatch(r"[0-9a-f]{64}\.body", child.name) is None:
                    raise ConditionalCacheIntegrityError("unknown conditional request cache entry")
                total += child.stat().st_size
        return total

    def load(
        self,
        url: str,
        params: Mapping[str, object] | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> CachedConditionalResponse | None:
        key = request_identity(url, params, request_headers)
        with self._locked(key) as directory:
            manifest_path = directory / "response.v1.json"
            _safe_path(manifest_path, parents=False)
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConditionalCacheIntegrityError("conditional manifest unreadable") from exc
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("request_key") != key
                or payload.get("url") != str(url)
                or payload.get("params") != _canonical_params(params)
                or str(payload.get("request_headers_sha256") or "")
                != _request_headers_sha256(request_headers)
            ):
                raise ConditionalCacheIntegrityError("conditional manifest identity mismatch")
            digest = payload.get("body_sha256")
            size = payload.get("body_size")
            if (
                not isinstance(digest, str)
                or _HEX_SHA256.fullmatch(digest) is None
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > self.max_response_bytes
            ):
                raise ConditionalCacheIntegrityError("conditional manifest body metadata invalid")
            body_path = directory / f"{digest}.body"
            _safe_path(body_path, parents=False)
            try:
                if not body_path.is_file():
                    raise ConditionalCacheIntegrityError("conditional body is not a regular file")
                body = body_path.read_bytes()
            except OSError as exc:
                raise ConditionalCacheIntegrityError("conditional body missing") from exc
            if len(body) != size or _sha256(body) != digest:
                raise ConditionalCacheIntegrityError("conditional body hash mismatch")
            return CachedConditionalResponse(
                request_key=key,
                body=body,
                etag=str(payload.get("etag") or ""),
                last_modified=str(payload.get("last_modified") or ""),
                stored_at=str(payload.get("stored_at") or ""),
            )

    def store(
        self,
        url: str,
        params: Mapping[str, object] | None,
        body: bytes,
        response_headers: Mapping[str, str],
        *,
        fetched_at: str,
        request_headers: Mapping[str, str] | None = None,
    ) -> CachedConditionalResponse:
        if not body:
            raise ValueError("conditional cache refuses an empty body")
        if len(body) > self.max_response_bytes:
            raise ConditionalCacheBudgetExceeded("conditional response exceeds response budget")
        key = request_identity(url, params, request_headers)
        digest = _sha256(body)
        etag = _header(response_headers, "etag")
        last_modified = _header(response_headers, "last-modified")
        with self._budget_locked(), self._locked(key) as directory:
            body_path = directory / f"{digest}.body"
            _safe_path(body_path, parents=False)
            if body_path.exists() and not body_path.is_file():
                raise ConditionalCacheIntegrityError("conditional body is not a regular file")
            existing_body = body_path.read_bytes() if body_path.exists() else None
            if existing_body != body:
                # Atomic replacement temporarily holds both the damaged old body
                # and the verified replacement. Budget that peak, not final size.
                projected = self._source_usage() + len(body)
                if projected > self.max_source_bytes:
                    raise ConditionalCacheBudgetExceeded("conditional source cache budget exceeded")
                # A fresh bounded 200 is authoritative repair evidence for a
                # damaged body whose content-addressed filename matches it.
                _atomic_write_bytes(body_path, body)
            payload = {
                "version": 1,
                "request_key": key,
                "url": str(url),
                "params": _canonical_params(params),
                "request_headers_sha256": _request_headers_sha256(request_headers),
                "body_sha256": digest,
                "body_size": len(body),
                "etag": etag,
                "last_modified": last_modified,
                "stored_at": fetched_at or datetime.now(timezone.utc).isoformat(),
            }
            manifest_path = directory / "response.v1.json"
            _safe_path(manifest_path, parents=False)
            atomic_write_json(manifest_path, payload, separators=(",", ":"))
        return CachedConditionalResponse(key, body, etag, last_modified, payload["stored_at"])


def _coerce_result(url: str, raw: object) -> ConditionalFetchResult:
    def value(name: str, default: Any = None) -> Any:
        return raw.get(name, default) if isinstance(raw, Mapping) else getattr(raw, name, default)

    is_response = not isinstance(raw, Mapping) or any(
        key in raw for key in ("status_code", "status", "text", "body", "error", "error_kind")
    )
    if not is_response:
        body = _body(raw)
        status = 200
        response_headers: dict[str, str] = {}
    else:
        body = _body(value("body", value("text", value("html", ""))))
        status_value = value("status_code", value("status"))
        status = status_value if isinstance(status_value, int) else None
        response_headers = _headers(value("response_headers", value("headers")))
    return ConditionalFetchResult(
        url=url,
        body=body,
        status_code=status,
        response_headers=response_headers,
        fetched_at=str(value("fetched_at", datetime.now(timezone.utc).isoformat())),
        error=str(value("error", "") or ""),
        error_kind=str(value("error_kind", "") or ""),
        attempts=max(1, int(value("attempts", 1) or 1)),
        elapsed_ms=max(0, int(value("elapsed_ms", 0) or 0)),
        backend=str(value("backend", "static_http") or "static_http"),
        fallback_used=bool(value("fallback_used", False)),
        fallback_from=str(value("fallback_from", "") or ""),
    )


def fetch_conditional(
    fetcher: Callable[..., object],
    url: str,
    *,
    cache: ConditionalResponseCache,
    params: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    fetch_kwargs: Mapping[str, object] | None = None,
) -> ConditionalFetchResult:
    """Fetch once conditionally; recover a cacheless 304 with one plain GET."""

    base_headers = {str(key): str(value) for key, value in (headers or {}).items()}
    try:
        cached = cache.load(url, params, base_headers)
    except ConditionalCacheIntegrityError:
        cached = None
    request_headers = dict(base_headers)
    if cached is not None:
        if cached.etag:
            request_headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            request_headers["If-Modified-Since"] = cached.last_modified
    kwargs = dict(fetch_kwargs or {})
    kwargs.update(headers=request_headers, params=dict(params or {}))
    first = _coerce_result(url, fetcher(url, **kwargs))
    total_attempts = first.attempts
    total_elapsed_ms = first.elapsed_ms
    result = first
    if first.status_code == 304 and cached is None:
        plain_headers = {
            key: value
            for key, value in request_headers.items()
            if key.casefold() not in {"if-none-match", "if-modified-since"}
        }
        kwargs.update(headers=plain_headers)
        result = _coerce_result(url, fetcher(url, **kwargs))
        total_attempts += result.attempts
        total_elapsed_ms += result.elapsed_ms
    if (
        result.status_code == 304
        and cached is not None
        and not result.error
        and not result.error_kind
    ):
        response_headers = dict(result.response_headers)
        etag = _header(response_headers, "etag") or cached.etag
        last_modified = _header(response_headers, "last-modified") or cached.last_modified
        if etag != cached.etag or last_modified != cached.last_modified:
            saved = cache.store(
                url,
                params,
                cached.body,
                {"ETag": etag, "Last-Modified": last_modified},
                fetched_at=result.fetched_at,
                request_headers=base_headers,
            )
            etag, last_modified = saved.etag, saved.last_modified
        return ConditionalFetchResult(
            **{
                **result.__dict__,
                "body": cached.body,
                "attempts": total_attempts,
                "elapsed_ms": total_elapsed_ms,
                "request_key": cached.request_key,
                "etag": etag,
                "last_modified": last_modified,
                "not_modified": True,
                "from_cache": True,
            }
        )
    if result.status_code == 200 and not result.error and result.body:
        saved = cache.store(
            url,
            params,
            result.body,
            result.response_headers,
            fetched_at=result.fetched_at,
            request_headers=base_headers,
        )
        return ConditionalFetchResult(
            **{
                **result.__dict__,
                "attempts": total_attempts,
                "elapsed_ms": total_elapsed_ms,
                "request_key": saved.request_key,
                "etag": saved.etag,
                "last_modified": saved.last_modified,
            }
        )
    return ConditionalFetchResult(
        **{
            **result.__dict__,
            "attempts": total_attempts,
            "elapsed_ms": total_elapsed_ms,
            "request_key": request_identity(url, params, base_headers),
        }
    )


__all__ = [
    "CachedConditionalResponse",
    "ConditionalCacheBudgetExceeded",
    "ConditionalCacheIntegrityError",
    "ConditionalFetchResult",
    "ConditionalResponseCache",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_SOURCE_BYTES",
    "fetch_conditional",
    "parse_retry_after_seconds",
    "request_identity",
]
