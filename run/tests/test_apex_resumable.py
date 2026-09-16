from threading import Event, Thread
from types import SimpleNamespace

import pytest

from hextech.infrastructure.persistence.raw_responses import RawResponseCache
from hextech.infrastructure.sources.apex import fetcher, service
from hextech.infrastructure.transport.scrapling_client import ScraplingFetchResult, FetchResult
from hextech.modules.acquisition.champion_downloads import DownloadContext


URL = "https://apexlol.info/zh/champions/Vi"
HTML = '<html><a href="/champions/Vi">Vi</a><p>0 条联动</p></html>'


def response(*, text=HTML, status=200, error="", error_kind="", backend="http", headers=None):
    return ScraplingFetchResult(URL, text, status, "fixture", error,
                               error_kind=error_kind, backend=backend,
                               response_headers=dict(headers or {}))


def test_raw_resume_preserves_http_backend(tmp_path, monkeypatch):
    raw = RawResponseCache(tmp_path, source="apex", revision="daily-fixture")
    calls = []
    monkeypatch.setattr(fetcher, "fetch_text", lambda *a, **k: calls.append(a) or response())
    assert fetcher.ApexSource(raw_cache=raw).fetch(URL).text == HTML
    assert fetcher.ApexSource(raw_cache=raw).fetch(URL).source == "http"
    assert len(calls) == 1
    assert raw.get(URL + "#apex-backend=http") == HTML.encode()


def test_browser_success_replayed_without_browser(tmp_path, monkeypatch):
    raw = RawResponseCache(tmp_path, source="apex", revision="daily-fixture")
    calls = []
    monkeypatch.setattr(fetcher, "fetch_text", lambda *a, **k: response(text="<html>shell</html>"))
    monkeypatch.setattr(fetcher, "fetch_browser_page", lambda *a, **k: calls.append(a) or
                        FetchResult(URL, HTML, None, 200, "fixture", None, backend="browser"))
    assert fetcher.ApexSource(raw_cache=raw).fetch(URL, allow_browser=True).source == "browser"
    assert fetcher.ApexSource(raw_cache=raw, allow_browser=lambda: False).fetch(URL).source == "browser"
    assert len(calls) == 1
    assert raw.status()["count"] == 1


@pytest.mark.parametrize("status,error,error_kind", [(403, "http_403", ""), (429, "http_429", ""),
                                                    (0, "host circuit open", "circuit_open"),
                                                    (503, "timeout", "timeout")])
def test_failed_responses_not_cached_and_no_nested_retry(tmp_path, monkeypatch, status, error, error_kind):
    raw = RawResponseCache(tmp_path, source="apex", revision="daily-fixture")
    calls = []
    monkeypatch.setattr(fetcher, "fetch_text", lambda *a, **k: calls.append(a) or
                        response(text="blocked", status=status, error=error, error_kind=error_kind))
    browser = []
    monkeypatch.setattr(fetcher, "fetch_browser_page", lambda *a, **k: browser.append(a))
    source = fetcher.ApexSource(raw_cache=raw)
    # 503 test covers the plain transport path; denied/circuit must never browser.
    source.fetch(URL, allow_browser=status != 503)
    assert len(calls) == 1
    assert browser == []
    assert raw.status()["count"] == 0


def test_browser_gate_is_read_at_fallback_time(monkeypatch):
    state = {"allowed": True}
    def plain(*a, **k):
        state["allowed"] = False
        return response(text="<html>shell</html>")
    monkeypatch.setattr(fetcher, "fetch_text", plain)
    monkeypatch.setattr(fetcher, "fetch_browser_page", lambda *a, **k: pytest.fail("browser forbidden"))
    fetcher.ApexSource(allow_browser=lambda: state["allowed"]).fetch(URL, allow_browser=True)


def test_stop_during_static_request_preserves_completed_bytes(tmp_path, monkeypatch):
    raw = RawResponseCache(tmp_path, source="apex", revision="daily-fixture")
    stop = Event()
    def plain(*a, **k):
        stop.set()
        return response()
    monkeypatch.setattr(fetcher, "fetch_text", plain)
    source = fetcher.ApexSource(raw_cache=raw, allow_browser=lambda: not stop.is_set())
    assert source.fetch(URL, allow_browser=True).text == HTML
    assert stop.is_set()
    assert raw.get(URL + "#apex-backend=http") == HTML.encode()


def prepare_main(
    monkeypatch,
    tmp_path,
    *,
    stop=None,
    status=200,
    not_modified=False,
    response_headers=None,
    missing_suffix="",
):
    calls = []
    gates = []
    monkeypatch.setattr(service, "load_active_catalog", lambda: SimpleNamespace(root=tmp_path, generation_id="g", content_sha256="a" * 64))
    monkeypatch.setattr(service, "load_champion_core_data", lambda root: {})
    monkeypatch.setattr(service, "build_core_info", lambda data: {"1": object(), "2": object()})
    monkeypatch.setattr(service, "build_champion_slug_map", lambda data: {"1": "Vi", "2": "Jinx"})
    monkeypatch.setattr(service, "build_champion_lookup", lambda data: {})
    monkeypatch.setattr(service, "build_augment_name_map_from_static", lambda root: {})
    monkeypatch.setattr(service, "_extract_and_classify_champion", lambda *a, **k:
                        ([], {}, service.classify_apex_page(HTML, expected_slug="Vi", entry_count=0, status_code=200)))
    class Source:
        base_url = "https://apexlol.info/zh"
        def __init__(self, **kwargs):
            self.gate = kwargs.get("allow_browser", lambda: True)
        def fetch(self, url, **kwargs):
            calls.append(url)
            gates.append(self.gate())
            if stop is not None:
                stop.set()
            if missing_suffix and url.endswith(missing_suffix):
                return None
            return service.FetchedResource(
                url,
                HTML,
                "http",
                status,
                None if status in {200, 304} else "failed",
                not_modified=not_modified,
                response_headers=dict(response_headers or {}),
            )
        def close(self):
            pass
    monkeypatch.setattr(service, "ApexSource", Source)
    monkeypatch.setattr(service, "SynergyWriter", lambda data: SimpleNamespace(build_payload=lambda x: {}))
    monkeypatch.setattr(service, "summarize_synergy_payload", lambda payload: {"non_empty_heroes": 0, "synergy_entries": 0})
    monkeypatch.setattr(service, "write_run_diagnostics", lambda *a, **k: None)
    monkeypatch.setattr(service, "publish_apex_run", lambda *a, **k: pytest.fail("unexpected publish"))
    return calls, gates


def test_main_priority_ingame_and_stop_between_requests(tmp_path, monkeypatch):
    stop = Event()
    calls, gates = prepare_main(monkeypatch, tmp_path, stop=stop)
    result = service.main(dry_run=True, stop_event=stop,
                          context=lambda: DownloadContext(champion_id="2", in_game=True))
    assert result["cancelled"] and not result["published"]
    assert len(calls) == 1 and calls[0].endswith("/Jinx")
    assert gates == [False]


def test_main_no_redundant_failed_tail_retry(tmp_path, monkeypatch):
    calls, _ = prepare_main(monkeypatch, tmp_path, status=503)
    result = service.main(dry_run=True)
    assert "error" not in result and not result["published"]
    assert len(calls) == 2


def test_main_default_constructor_remains_noargs_compatible(tmp_path, monkeypatch):
    calls, _ = prepare_main(monkeypatch, tmp_path, status=503)
    original = service.ApexSource
    class LegacyNoargsSource(original):
        def __init__(self):
            super().__init__()
    monkeypatch.setattr(service, "ApexSource", LegacyNoargsSource)
    result = service.main(dry_run=True)
    assert "error" not in result and len(calls) == 2


def test_main_paused_background_cooperates_with_stop(tmp_path, monkeypatch):
    stop = Event()
    observed = Event()
    calls, _ = prepare_main(monkeypatch, tmp_path)
    def context():
        observed.set()
        return DownloadContext(pause_background=True)
    results = []
    thread = Thread(target=lambda: results.append(service.main(dry_run=True, stop_event=stop, context=context)))
    thread.start()
    assert observed.wait(5)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert calls == [] and results[0]["cancelled"]


def test_preset_stop_does_not_load_catalog(monkeypatch):
    stop = Event()
    stop.set()
    monkeypatch.setattr(service, "load_active_catalog", lambda: pytest.fail("already cancelled"))
    assert service.main(stop_event=stop)["cancelled"]


def test_main_reprocessing_passes_original_raw_data_at(tmp_path, monkeypatch):
    prepare_main(monkeypatch, tmp_path)
    raw = RawResponseCache(tmp_path / "raw", source="apex", revision="fixed")
    raw.put(URL, HTML.encode())
    original_time = raw.data_at()
    monkeypatch.setattr(service, "summarize_synergy_payload", lambda payload: {"non_empty_heroes": 1, "synergy_entries": 1})
    stamps = []
    monkeypatch.setattr(service, "publish_apex_run", lambda *a, **k: stamps.append(k["data_at"]) or ("fixture.json", None))
    monkeypatch.setattr(service, "utc_now_iso", lambda: "2027-01-01T00:00:00Z")
    assert service.main(dry_run=False, raw_cache=raw)["published"]
    monkeypatch.setattr(service, "utc_now_iso", lambda: "2028-01-01T00:00:00Z")
    assert service.main(dry_run=False, raw_cache=raw)["published"]
    assert stamps == [original_time, original_time]
    assert service.main(dry_run=False)["published"]
    assert stamps[-1] == "2028-01-01T00:00:00Z"


def test_publisher_carries_optional_data_at_metadata(tmp_path, monkeypatch):
    from hextech.infrastructure.sources.apex import publisher
    from hextech.contracts import ItemOutcome
    monkeypatch.setattr(publisher, "resolve_current_artifact", lambda source: None)
    monkeypatch.setattr(publisher, "validate_apex_run", lambda *a, **k: {"record_count": 1})
    monkeypatch.setattr(publisher, "source_run_artifact_path", lambda source, run, relative: tmp_path / f"{run}.json")
    monkeypatch.setattr(publisher, "load_active_catalog", lambda: SimpleNamespace(generation_id="catalog", content_sha256="a" * 64))
    monkeypatch.setattr(publisher, "publish_source_run", lambda *a, **k: None)
    kwargs = {"outcomes": (ItemOutcome("1", "success", "detail", record_count=1),),
              "record_count": 1, "started_at": "2026-01-01T00:00:00Z"}
    _, old = publisher.publish_apex_run({"1": {}}, run_id="old", data_at="2020-01-01T00:00:00Z", **kwargs)
    assert old.metadata["data_at"] == "2020-01-01T00:00:00Z"
    _, new = publisher.publish_apex_run({"1": {}}, run_id="new", **kwargs)
    assert new.metadata["data_at"] == kwargs["started_at"]


def test_progress_counts_come_from_actual_finished_hero_tasks(tmp_path, monkeypatch):
    prepare_main(monkeypatch, tmp_path, status=503)
    progress = []
    service.main(dry_run=True, on_progress=lambda done, total, phase: progress.append((done, total, phase)))
    assert progress[:2] == [(1, 2, "download"), (2, 2, "download")]
    assert progress[-1] == (2, 2, "candidate")


def test_conditional_apex_304_reuses_verified_body(tmp_path, monkeypatch):
    from hextech.infrastructure.transport.conditional_response import ConditionalResponseCache

    requests = []

    def request(*_args, **kwargs):
        requests.append(dict(kwargs.get("headers") or {}))
        if len(requests) == 1:
            return response(headers={"ETag": '"apex-v1"'})
        return response(text="", status=304, headers={"ETag": '"apex-v1"'})

    monkeypatch.setattr(fetcher, "fetch_text", request)
    cache = ConditionalResponseCache(tmp_path, source="apex")
    first = fetcher.ApexSource(conditional_cache=cache).fetch(URL)
    second = fetcher.ApexSource(conditional_cache=cache).fetch(URL)

    assert first is not None and first.text == HTML and not first.not_modified
    assert second is not None and second.text == HTML and second.not_modified and second.from_cache
    assert requests[1]["If-None-Match"] == '"apex-v1"'


def test_all_apex_304_skips_projection_and_source_run(tmp_path, monkeypatch):
    prepare_main(monkeypatch, tmp_path, status=304, not_modified=True)
    monkeypatch.setattr(service, "upstream_revision", lambda _resources: "u" * 64)
    monkeypatch.setattr(
        service,
        "current_manifest",
        lambda: (
            {"run_id": "apex-current", "catalog_generation_id": "g", "catalog_sha256": "a" * 64},
            {
                "upstream_revision": "u" * 64,
                "applied_revision": "p" * 64,
                "parser_revision": service.APEX_PROJECTION_REVISION,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_extract_and_classify_champion",
        lambda *_a, **_k: pytest.fail("304 unchanged must not reparse"),
    )

    # Seven days at four-hour intervals: checks are allowed, projection/run creation is not.
    for _ in range(42):
        result = service.main(dry_run=False)
        assert result["success"] is True
        assert result["check_status"] == "up_to_date"
        assert result["reason"] == "not_stale"
        assert result["run_id"] == "apex-current"
        assert result["upstream_revision"] == result["applied_revision"] == "p" * 64
        assert result["representation_revision"] == "u" * 64


@pytest.mark.parametrize(
    ("status", "missing_suffix"),
    ((503, ""), (0, ""), (200, "/Jinx")),
)
def test_apex_incomplete_or_failed_check_cannot_claim_up_to_date(
    tmp_path, monkeypatch, status, missing_suffix
):
    prepare_main(
        monkeypatch,
        tmp_path,
        status=status,
        missing_suffix=missing_suffix,
    )
    monkeypatch.setattr(service, "upstream_revision", lambda _resources: "u" * 64)
    monkeypatch.setattr(
        service,
        "current_manifest",
        lambda: (
            {"run_id": "apex-current", "catalog_generation_id": "g", "catalog_sha256": "a" * 64},
            {
                "upstream_revision": "u" * 64,
                "applied_revision": "p" * 64,
                "parser_revision": service.APEX_PROJECTION_REVISION,
            },
        ),
    )

    result = service.main(dry_run=False)

    assert result["success"] is False
    assert result["check_status"] == "unknown"


def test_apex_source_result_honors_server_retry_after(tmp_path, monkeypatch):
    prepare_main(
        monkeypatch,
        tmp_path,
        status=429,
        response_headers={"Retry-After": "43200"},
    )

    result = service.main(dry_run=False)

    assert result["success"] is False
    assert result["retry_after_seconds"] == 43200


def test_apex_fetch_preserves_response_headers(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "fetch_text",
        lambda *_a, **_k: response(
            text="blocked",
            status=429,
            headers={"Retry-After": "43200"},
        ),
    )

    resource = fetcher.ApexSource().fetch(URL)

    assert resource is not None
    assert resource.response_headers["Retry-After"] == "43200"


def test_apex_200_same_semantics_does_not_publish_new_run(tmp_path, monkeypatch):
    prepare_main(monkeypatch, tmp_path)
    payload = {"1": {"synergy_items": [{"augment_names": ["测试"]}]}}
    monkeypatch.setattr(
        service,
        "SynergyWriter",
        lambda _data: SimpleNamespace(build_payload=lambda _entries: payload),
    )
    monkeypatch.setattr(
        service,
        "summarize_synergy_payload",
        lambda _payload: {"non_empty_heroes": 1, "synergy_entries": 1},
    )
    monkeypatch.setattr(
        service,
        "current_manifest",
        lambda: (
            {"run_id": "apex-current", "catalog_generation_id": "g", "catalog_sha256": "a" * 64},
            {
                "applied_revision": service.canonical_sha256(payload),
                "parser_revision": service.APEX_PROJECTION_REVISION,
            },
        ),
    )

    result = service.main(dry_run=False)

    assert result["success"] is True
    assert result["check_status"] == "up_to_date"
    assert result["published"] is False
    assert result["upstream_revision"] == result["applied_revision"]
    assert result["representation_revision"] != result["applied_revision"]


def test_apex_repeated_failed_revision_skips_expensive_parse(tmp_path, monkeypatch):
    from hextech.infrastructure.sources.apex.incremental import APEX_REPEAT_INVALID_RETRY_SECONDS

    prepare_main(monkeypatch, tmp_path, status=304, not_modified=True)
    monkeypatch.setattr(service, "upstream_revision", lambda _resources: "u" * 64)
    fingerprint = service.failure_fingerprint(
        "u" * 64, service.APEX_PROJECTION_REVISION, "g", "a" * 64
    )
    monkeypatch.setattr(
        service,
        "_extract_and_classify_champion",
        lambda *_a, **_k: pytest.fail("same failed revision must not reparse"),
    )

    result = service.main(dry_run=False, previous_failure_fingerprint=fingerprint)

    assert result["success"] is False
    assert result["reason_code"] == "repeated_invalid_content"
    assert result["retry_after_seconds"] == APEX_REPEAT_INVALID_RETRY_SECONDS
