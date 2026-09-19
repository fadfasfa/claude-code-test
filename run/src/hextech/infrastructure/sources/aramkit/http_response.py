"""Pure HTTP response normalization for ARAMKit; no transport or persistence."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from hextech.contracts import FetchAttempt, utc_now_iso
from hextech.contracts.models import FailureKind


@dataclass(frozen=True)
class _DetailResult:
    champion_id: str
    response: _Response | None
    normalized: Mapping[str, Any] | None = None
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.normalized is not None and not self.reason

    @property
    def retryable(self) -> bool:
        return self.response is not None and self.response.retryable


@dataclass(frozen=True)
class _Response:
    url: str
    body: bytes
    status_code: int | None
    error_kind: str
    error: str
    elapsed_ms: int
    attempts: int
    fetched_at: str
    response_headers: Mapping[str, str] | None = None
    not_modified: bool = False
    from_cache: bool = False
    backend: str = "static_http"

    @property
    def blocking(self) -> bool:
        return self.status_code in {403, 429} or self.error_kind in {
            FailureKind.HTTP_403.value,
            FailureKind.HTTP_429.value,
        }

    @property
    def retryable(self) -> bool:
        return self.failure_kind in {
            FailureKind.TIMEOUT,
            FailureKind.TLS_ERROR,
            FailureKind.NETWORK_ERROR,
            FailureKind.HTTP_5XX,
        }

    @property
    def failure_kind(self) -> FailureKind | None:
        if self.status_code == 403:
            return FailureKind.HTTP_403
        if self.status_code == 429:
            return FailureKind.HTTP_429
        if self.status_code is not None and 500 <= self.status_code <= 599:
            return FailureKind.HTTP_5XX
        if self.error_kind:
            try:
                return FailureKind(self.error_kind)
            except ValueError:
                return FailureKind.NETWORK_ERROR
        if self.status_code not in {200, 304} or not self.body:
            return FailureKind.INVALID_PAYLOAD
        return None

    def attempt(self) -> FetchAttempt:
        failure = self.failure_kind
        return FetchAttempt(
            url=self.url,
            backend=self.backend,
            status_code=self.status_code,
            elapsed_ms=max(0, self.elapsed_ms),
            attempts=max(1, self.attempts),
            failure_kind=failure,
            retryable=self.retryable,
            fetched_at=self.fetched_at,
            error=self.error,
        )


def _body_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b""


def _coerce_response(url: str, raw: object) -> _Response:
    if isinstance(raw, _Response):
        return raw
    now = utc_now_iso()
    if isinstance(raw, tuple) and len(raw) >= 2:
        status = raw[0] if isinstance(raw[0], int) or raw[0] is None else None
        error_kind = str(raw[2]) if len(raw) >= 3 else ""
        return _Response(url, _body_bytes(raw[1]), status, error_kind, error_kind, 0, 1, now)
    if isinstance(raw, (bytes, str, Mapping)):
        return _Response(url, _body_bytes(raw), 200, "", "", 0, 1, now)
    text = getattr(raw, "text", "")
    body = _body_bytes(text)
    status = getattr(raw, "status_code", None)
    return _Response(
        url=url,
        body=body,
        status_code=status if isinstance(status, int) else None,
        error_kind=str(getattr(raw, "error_kind", "") or ""),
        error=str(getattr(raw, "error", "") or ""),
        elapsed_ms=int(getattr(raw, "elapsed_ms", 0) or 0),
        attempts=int(getattr(raw, "attempts", 1) or 1),
        fetched_at=str(getattr(raw, "fetched_at", "") or now),
        response_headers=getattr(raw, "response_headers", None),
        not_modified=bool(getattr(raw, "not_modified", False)),
        from_cache=bool(getattr(raw, "from_cache", False)),
        backend=str(getattr(raw, "backend", "static_http") or "static_http"),
    )
