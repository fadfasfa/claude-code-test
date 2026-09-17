from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess

import pytest

from hextech.infrastructure.transport.conditional_response import (
    ConditionalCacheBudgetExceeded,
    ConditionalCacheIntegrityError,
    ConditionalResponseCache,
    fetch_conditional,
    parse_retry_after_seconds,
    request_identity,
)


class Fetcher:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def response(status, text="", **headers):
    return {
        "status_code": status,
        "text": text,
        "response_headers": headers,
        "fetched_at": "2026-09-15T00:00:00+00:00",
    }


def test_304_reuses_verified_body_and_sends_both_validators(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="aramkit")
    first = fetch_conditional(
        Fetcher([response(200, '{"value":1}', ETag='"v1"', **{"Last-Modified": "Mon"})]),
        "https://example.test/data",
        cache=cache,
        params={"dataset": "all"},
    )
    fetcher = Fetcher([response(304)])

    second = fetch_conditional(
        fetcher,
        "https://example.test/data",
        cache=cache,
        params={"dataset": "all"},
    )

    assert first.not_modified is False
    assert second.not_modified is True and second.from_cache is True
    assert second.body == b'{"value":1}' and second.status_code == 304
    assert fetcher.calls[0][1]["headers"] == {
        "If-None-Match": '"v1"',
        "If-Modified-Since": "Mon",
    }


def test_cacheless_304_gets_exactly_one_unconditional_recovery(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="apex")
    fetcher = Fetcher([response(304), response(200, "payload", ETag='"v2"')])

    result = fetch_conditional(fetcher, "https://example.test/data", cache=cache)

    assert result.body == b"payload" and result.attempts == 2
    assert len(fetcher.calls) == 2
    assert fetcher.calls[0][1]["headers"] == fetcher.calls[1][1]["headers"] == {}


def test_url_and_normalized_params_are_separate_request_identities(tmp_path: Path) -> None:
    assert request_identity("https://example.test", {"a": 1, "b": 2}) == request_identity(
        "https://example.test", {"b": 2, "a": 1}
    )
    assert request_identity("https://example.test", {"a": 1}) != request_identity(
        "https://example.test", {"a": 2}
    )

    cache = ConditionalResponseCache(tmp_path, source="params")
    fetch_conditional(
        Fetcher([response(200, "one", ETag='"one"')]),
        "https://example.test",
        cache=cache,
        params={"a": 1},
    )
    other = Fetcher([response(200, "two", ETag='"two"')])
    fetch_conditional(other, "https://example.test", cache=cache, params={"a": 2})
    assert "If-None-Match" not in other.calls[0][1]["headers"]


def test_damaged_cached_body_cannot_authorize_304(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="mayhem")
    url = "https://example.test/data"
    fetch_conditional(Fetcher([response(200, "old", ETag='"v1"')]), url, cache=cache)
    request_dir = cache.directory / request_identity(url)
    body_path = next(request_dir.glob("*.body"))
    body_path.write_bytes(b"damaged")
    fetcher = Fetcher([response(304), response(200, "old", ETag='"v2"')])

    result = fetch_conditional(fetcher, url, cache=cache)

    assert result.body == b"old" and result.not_modified is False
    assert len(fetcher.calls) == 2
    assert cache.load(url).body == b"old"


def test_representation_headers_partition_validators(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="vary")
    url = "https://example.test/data"
    fetch_conditional(
        Fetcher([response(200, "zh-body", ETag='"zh"', Vary="Accept-Language")]),
        url,
        cache=cache,
        headers={"Accept-Language": "zh-CN"},
    )
    english = Fetcher([response(200, "en-body", ETag='"en"', Vary="Accept-Language")])

    result = fetch_conditional(
        english,
        url,
        cache=cache,
        headers={"Accept-Language": "en-US"},
    )

    assert result.text == "en-body"
    assert "If-None-Match" not in english.calls[0][1]["headers"]
    assert request_identity(url, headers={"Accept-Language": "zh-CN"}) != request_identity(
        url, headers={"Accept-Language": "en-US"}
    )


def test_304_with_error_kind_cannot_authorize_cached_body(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="304-error")
    url = "https://example.test/data"
    fetch_conditional(Fetcher([response(200, "trusted", ETag='"v1"')]), url, cache=cache)
    failed = {
        **response(304),
        "error": "",
        "error_kind": "timeout",
    }

    result = fetch_conditional(Fetcher([failed]), url, cache=cache)

    assert result.not_modified is False
    assert result.body == b""
    assert result.error_kind == "timeout"


def test_response_and_source_budgets_refuse_growth_without_replacing_manifest(tmp_path: Path) -> None:
    response_limited = ConditionalResponseCache(
        tmp_path / "response",
        source="budget",
        max_response_bytes=3,
        max_source_bytes=10,
    )
    with pytest.raises(ConditionalCacheBudgetExceeded):
        response_limited.store("https://example.test/data", None, b"four", {}, fetched_at="fixture")
    assert not list(response_limited.directory.rglob("*.body"))

    source_limited = ConditionalResponseCache(
        tmp_path / "source",
        source="budget",
        max_response_bytes=4,
        max_source_bytes=5,
    )
    url = "https://example.test/data"
    source_limited.store(url, None, b"one", {"ETag": '"one"'}, fetched_at="fixture")
    before = source_limited.load(url)
    with pytest.raises(ConditionalCacheBudgetExceeded):
        source_limited.store(url, None, b"two", {"ETag": '"two"'}, fetched_at="fixture")

    assert before is not None and before.body == b"one"
    assert source_limited.load(url) == before
    assert len(list(source_limited.directory.rglob("*.body"))) == 1


def test_damaged_body_repair_accounts_for_atomic_write_peak(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="repair-peak", max_response_bytes=4, max_source_bytes=6)
    url = "https://example.test/data"
    cache.store(url, None, b"good", {"ETag": '"v1"'}, fetched_at="fixture")
    body = next(cache.directory.rglob("*.body"))
    manifest = next(cache.directory.rglob("response.v1.json"))
    before = manifest.read_bytes()
    body.write_bytes(b"bad!")
    # Final size would fit (4 bytes), but old + temporary replacement is 8.
    with pytest.raises(ConditionalCacheBudgetExceeded):
        cache.store(url, None, b"good", {"ETag": '"v2"'}, fetched_at="fixture")
    assert body.read_bytes() == b"bad!"
    assert manifest.read_bytes() == before


def test_source_budget_lock_prevents_concurrent_oversubscription(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(
        tmp_path,
        source="concurrent-budget",
        max_response_bytes=4,
        max_source_bytes=6,
    )

    def store(index: int) -> str:
        try:
            cache.store(
                f"https://example.test/{index}",
                None,
                b"four",
                {},
                fetched_at="fixture",
            )
            return "stored"
        except ConditionalCacheBudgetExceeded:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(store, (1, 2)))

    assert sorted(outcomes) == ["rejected", "stored"]
    assert sum(path.stat().st_size for path in cache.directory.rglob("*.body")) == 4


def test_malformed_digest_is_rejected_before_path_use(tmp_path: Path) -> None:
    cache = ConditionalResponseCache(tmp_path, source="digest")
    url = "https://example.test/data"
    key = request_identity(url)
    request_dir = cache.directory / key
    request_dir.mkdir(parents=True)
    malformed = "../" + "a" * 61
    (request_dir / "response.v1.json").write_text(
        json.dumps({
            "version": 1,
            "request_key": key,
            "url": url,
            "params": {},
            "request_headers_sha256": "",
            "body_sha256": malformed,
            "body_size": 1,
        }),
        encoding="utf-8",
    )

    with pytest.raises(ConditionalCacheIntegrityError, match="body metadata"):
        cache.load(url)


def test_reparse_cache_root_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "cache-link"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("host does not allow test junctions")
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(ConditionalCacheIntegrityError, match="reparse/link"):
            ConditionalResponseCache(link, source="unsafe")
    finally:
        link.rmdir()


def test_retry_after_preserves_server_delta_and_http_date_beyond_local_backoff() -> None:
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)

    assert parse_retry_after_seconds({"Retry-After": "120"}, now=now) == 120
    assert parse_retry_after_seconds(
        {"retry-after": "Tue, 15 Sep 2026 12:00:00 GMT"}, now=now
    ) == 12 * 60 * 60
    assert parse_retry_after_seconds({"Retry-After": str(12 * 60 * 60)}, now=now) == 12 * 60 * 60
    assert parse_retry_after_seconds(
        {"Retry-After": "999999"}, now=now, maximum_seconds=7 * 24 * 60 * 60
    ) == 7 * 24 * 60 * 60
    assert parse_retry_after_seconds({"Retry-After": "invalid"}, now=now) is None
