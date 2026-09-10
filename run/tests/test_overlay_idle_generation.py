"""空闲期元数据跟随 verified current，不能改变对局固定代或阻塞 GUI。"""
import threading
import time
from types import SimpleNamespace
from hextech.interfaces.overlay.host_data_preparation import OverlayDataPreparation


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.005)
    raise AssertionError("background result did not converge")


class Source:
    generation = "g1"
    fail = False
    def __init__(self):
        self.threads = []
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
    def open_view(self):
        self.threads.append(threading.get_ident())
        self.entered.set()
        assert self.release.wait(2)
        if self.fail:
            return None
        identity = self.generation
        return SimpleNamespace(status=lambda: {"generation_id": identity, "state": "ready"},
            get_overlay_hints=lambda: {"source": {}, "hints": {}, "name_index": {}})
    def read_hint_cache(self):
        raise AssertionError("must use verified view")


def test_idle_current_refresh_is_background_bounded_and_does_not_pin_game():
    source = Source()
    worker = OverlayDataPreparation(source)
    try:
        assert worker.request_idle_refresh(now=100)
        wait_for(lambda: worker.status()["bootstrap_generation_id"] == "g1")
        source.generation = "g2"
        assert worker.request_idle_refresh(now=100.5) is False
        assert worker.request_idle_refresh(now=101)
        wait_for(lambda: worker.status()["bootstrap_generation_id"] == "g2")
        assert worker._generation.status()["generation_id"] == ""
        assert set(source.threads) == {worker._thread.ident}
        assert threading.get_ident() not in source.threads
        source.fail = True
        assert worker.request_idle_refresh(now=102)
        wait_for(lambda: worker.status()["error_type"] == "snapshot_unavailable")
        assert worker.status()["bootstrap_generation_id"] == "g2"
    finally:
        worker.close()


def test_idle_refresh_does_not_publish_into_new_game_or_after_close():
    source = Source()
    source.release.clear()
    worker = OverlayDataPreparation(source)
    try:
        assert worker.request_idle_refresh(now=100)
        assert source.entered.wait(1)
        with worker._condition:
            worker._current_key = ("active-game",)
            worker._version += 1
        assert worker.request_idle_refresh(now=102) is False
        source.release.set()
        worker.close()
        assert worker.status()["bootstrap_generation_id"] == ""
        assert worker.request_idle_refresh(now=105) is False
    finally:
        source.release.set()
        worker.close()


def test_idle_refresh_reports_failed_current_instead_of_relabeling_previous():
    source = Source()
    worker = OverlayDataPreparation(source)
    try:
        assert worker.request_idle_refresh(now=100)
        wait_for(lambda: worker.status()["bootstrap_generation_id"] == "g1")
        source.open_view = lambda: SimpleNamespace(status=lambda: {
            "generation_id": "previous", "state": "degraded", "failed_generation_id": "broken-current"})
        assert worker.request_idle_refresh(now=101)
        wait_for(lambda: worker.status()["error_type"] == "current_snapshot_invalid")
        assert worker.status()["bootstrap_generation_id"] == "g1"
    finally:
        worker.close()


def test_real_snapshot_fallback_does_not_relabel_idle_bootstrap(tmp_path):
    import json
    from test_cohort_seed import _fixture_runtime
    from hextech.modules.data.generation import DataSnapshotClient

    runtime, data = _fixture_runtime(tmp_path)
    client = DataSnapshotClient(runtime / "snapshots")
    source = Source()
    source.open_view = client.open_view
    worker = OverlayDataPreparation(source)
    try:
        assert worker.request_idle_refresh(now=100)
        wait_for(lambda: worker.status()["bootstrap_generation_id"] == data["generation_id"])
        # Isolated fixture pointers only; original immutable fixture remains intact.
        (runtime / "snapshots/previous.v2.json").write_text(json.dumps({
            "schema_version": 2, "generation_id": data["generation_id"]}), encoding="utf-8")
        (runtime / "snapshots/current.v2.json").write_text(json.dumps({
            "schema_version": 2, "current_generation_id": "20990101T000000-aaaaaaaaaa"}), encoding="utf-8")
        assert client.open_view().status()["failed_generation_id"] == "20990101T000000-aaaaaaaaaa"
        assert worker.request_idle_refresh(now=101)
        wait_for(lambda: worker.status()["error_type"] == "current_snapshot_invalid")
        assert worker.status()["bootstrap_generation_id"] == data["generation_id"]
    finally:
        worker.close()
