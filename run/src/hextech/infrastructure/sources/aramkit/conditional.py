"""Conditional versions discovery for the mutable ARAMKit index."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from hextech.infrastructure.transport.conditional_response import (
    ConditionalResponseCache,
    fetch_conditional,
)


Fetcher = Callable[..., object]


def with_conditional_versions(
    fetcher: Fetcher,
    cache_root: Path | None,
    *,
    versions_url: str,
) -> Fetcher:
    if cache_root is None:
        return fetcher
    cache = ConditionalResponseCache(cache_root / "conditional-http", source="aramkit")

    def wrapped(url: str, **kwargs: object) -> object:
        if url != versions_url:
            return fetcher(url, **kwargs)
        headers = kwargs.pop("headers", None)
        return fetch_conditional(
            fetcher,
            url,
            cache=cache,
            headers=headers if isinstance(headers, Mapping) else None,
            fetch_kwargs=kwargs,
        )

    return wrapped


__all__ = ["with_conditional_versions"]
