"""Per-check HTTP evidence, including retries and raw-cache reuse, without URLs on disk."""
from functools import wraps
import threading

from .http_response import _coerce_response


def measure_http(default_fetcher):
    def decorate(function):
        @wraps(function)
        def measured(**kwargs):
            delegate = kwargs.get("fetcher") or default_fetcher
            lock = threading.Lock()
            counters = {"requests": 0, "failed_requests": 0, "repeated_url_requests": 0,
                        "retries": 0, "not_modified": 0, "raw_cache_hits": 0}
            seen: set[str] = set()
            failed: set[str] = set()

            def fetch(url, **options):
                with lock:
                    counters["requests"] += 1
                    counters["repeated_url_requests"] += int(url in seen)
                    counters["retries"] += int(url in failed)
                    seen.add(url)
                try:
                    response = delegate(url, **options)
                    parsed = _coerce_response(url, response)
                    with lock:
                        counters["not_modified"] += int(parsed.status_code == 304)
                        bad = parsed.failure_kind is not None and parsed.status_code != 304
                        counters["failed_requests"] += int(bad)
                        failed.add(url) if bad else failed.discard(url)
                    return response
                except Exception:
                    with lock:
                        counters["failed_requests"] += 1
                        failed.add(url)
                    raise

            def cache_hit():
                with lock:
                    counters["raw_cache_hits"] += 1

            fetch.record_cache_hit = cache_hit
            kwargs["fetcher"] = fetch
            try:
                result = function(**kwargs)
            except Exception as exc:
                exc.http_summary = dict(counters)
                raise
            return {**result, "http_summary": dict(counters)}
        return measured
    return decorate
