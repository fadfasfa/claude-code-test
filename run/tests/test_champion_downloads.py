from threading import Event, Lock

from hextech.modules.acquisition.champion_downloads import ChampionDownloads, DownloadContext


def test_priority_is_read_at_each_dequeue_and_does_not_restart_completed():
    context = DownloadContext("3", in_game=True)
    seen = []

    def fetch(champion):
        nonlocal context
        seen.append(champion)
        context = DownloadContext("2", in_game=True)
        return champion

    result = ChampionDownloads(lambda: context).run(["1", "2", "3", "3"], fetch, stop=Event())
    assert seen == ["3", "2", "1"]
    assert len(result) == 3


def test_game_mode_is_one_request_and_finish_survives_context_change():
    lock = Lock()
    active = 0
    maximum = 0

    def fetch(champion):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        with lock:
            active -= 1
        return champion

    result = ChampionDownloads(lambda: DownloadContext(in_game=True)).run(
        [str(i) for i in range(8)], fetch, stop=Event())
    assert len(result) == 8
    assert maximum == 1


def test_stop_does_not_discard_inflight_result():
    stop = Event()

    def fetch(champion):
        stop.set()
        return champion

    result = ChampionDownloads(lambda: DownloadContext(in_game=True)).run(["1", "2"], fetch, stop=stop)
    assert result == {"1": "1"}
