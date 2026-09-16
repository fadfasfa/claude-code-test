"""Conditional body reuse and validation-failure identity for Blitz."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

from hextech.infrastructure.transport.conditional_response import (
    ConditionalResponseCache,
    fetch_conditional,
)


PARSER_REVISION = "blitz-schema-v1"
Fetcher = Callable[..., object]


def with_conditional_response(fetcher: Fetcher, cache_root: Path | None) -> Fetcher:
    if cache_root is None:
        return fetcher
    cache = ConditionalResponseCache(cache_root / "conditional-http", source="blitz")

    def wrapped(url: str, **kwargs: object) -> object:
        headers = kwargs.pop("headers", None)
        params = kwargs.pop("params", None)
        return fetch_conditional(
            fetcher,
            url,
            cache=cache,
            params=params if isinstance(params, Mapping) else None,
            headers=headers if isinstance(headers, Mapping) else None,
            fetch_kwargs=kwargs,
        )

    return wrapped


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def failure_identity(
    text: str,
    *,
    catalog_generation_id: str,
    catalog_sha256: str,
) -> str:
    payload = {
        "content_sha256": content_sha256(text),
        "parser_revision": PARSER_REVISION,
        "catalog_generation_id": catalog_generation_id,
        "catalog_sha256": catalog_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PARSER_REVISION",
    "content_sha256",
    "failure_identity",
    "with_conditional_response",
]
