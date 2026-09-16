from threading import Event

from hextech.infrastructure.sources.aramkit import service
from hextech.modules.acquisition.champion_downloads import DownloadContext
from test_aramkit_source import _binding, _happy_fetcher, _urls
import test_aramkit_source as fixtures

isolated_sources = fixtures.isolated_sources


def test_raw_responses_survive_cancel_and_current_hero_is_first(isolated_sources):
    stop = Event()
    network = _happy_fetcher(["1", "2", "3"])
    fetched = []
    ready = []

    def fetch(url, **kwargs):
        fetched.append(url)
        return network(url, **kwargs)

    def detail(version, hero, payload):
        ready.append(hero)
        stop.set()

    args = dict(fetcher=fetch, catalog_binding=_binding(["1", "2", "3"]),
                raw_cache_root=isolated_sources / "raw-responses",
                context=lambda: DownloadContext("2", in_game=True))
    first = service.refresh_aramkit(**args, stop_event=stop, on_detail=detail)
    assert not first["success"]
    assert ready == ["2"]
    detail_url = _urls(["1", "2", "3"])[2]["2"]
    count = fetched.count(detail_url)
    second = service.refresh_aramkit(**args)
    assert second["success"]
    assert fetched.count(detail_url) == count


def test_rankings_callback_precedes_details(isolated_sources):
    events = []
    result = service.refresh_aramkit(
        fetcher=_happy_fetcher(["1"]), catalog_binding=_binding(["1"]),
        on_rankings=lambda version, rows: events.append("ranking"),
        on_detail=lambda version, hero, data: events.append(hero),
    )
    assert result["success"]
    assert events == ["ranking", "1"]


def test_incremental_completion_does_not_write_duplicate_full_run(isolated_sources, monkeypatch):
    writes = []
    monkeypatch.setattr(service, "_write_artifact", lambda *args: writes.append(args))
    result = service.refresh_aramkit(fetcher=_happy_fetcher(["1"]), catalog_binding=_binding(["1"]),
        incremental_only=True, on_rankings=lambda *_: None, on_detail=lambda *_: None)
    assert result["success"] and result["reason"] == "units_complete"
    assert writes == []
