"""测试 Hextech detail pass 的超时收口与防重叠行为。"""

from __future__ import annotations

import threading
import time

import pytest


def test_detail_pass_runner_rejects_new_run_while_previous_workers_are_draining():
    from hextech.scraping.hextech.detail_runner import DetailPassRunner

    release = threading.Event()
    runner = DetailPassRunner()

    def blocked_worker(item: int) -> int:
        release.wait(timeout=2)
        return item

    first = runner.run(
        [1],
        worker=blocked_worker,
        max_workers=1,
        timeout_seconds=0.01,
    )
    second = runner.run(
        [2],
        worker=lambda item: item,
        max_workers=1,
        timeout_seconds=0.01,
    )

    assert first.status == "timed_out"
    assert first.pending_items == [1]
    assert second.status == "draining"

    release.set()
    deadline = time.time() + 2
    while runner.is_draining() and time.time() < deadline:
        time.sleep(0.01)

    assert runner.is_draining() is False
    third = runner.run(
        [3],
        worker=lambda item: item,
        max_workers=1,
        timeout_seconds=1,
    )
    assert third.status == "completed"
    assert third.results == [(3, 3)]


def test_detail_pass_runner_reports_worker_exceptions_and_releases_ownership():
    from hextech.scraping.hextech.detail_runner import DetailPassRunner

    runner = DetailPassRunner()

    def fail_worker(_item: int) -> int:
        raise ValueError("detail failed")

    result = runner.run(
        [1],
        worker=fail_worker,
        max_workers=1,
        timeout_seconds=1,
    )

    assert result.status == "completed"
    assert result.results == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == 1
    assert isinstance(result.errors[0][1], ValueError)
    assert runner.is_draining() is False


def test_detail_pass_runner_returns_stopped_when_stop_event_is_set():
    from hextech.scraping.hextech.detail_runner import DetailPassRunner

    runner = DetailPassRunner()
    stop_event = threading.Event()
    stop_event.set()

    result = runner.run(
        [1],
        worker=lambda item: item,
        max_workers=1,
        timeout_seconds=1,
        stop_event=stop_event,
    )

    assert result.status == "stopped"
    deadline = time.time() + 2
    while runner.is_draining() and time.time() < deadline:
        time.sleep(0.01)
    assert runner.is_draining() is False


def test_detail_pass_runner_drains_remaining_workers_after_base_exception():
    from hextech.scraping.hextech.detail_runner import DetailPassRunner

    runner = DetailPassRunner()
    release = threading.Event()
    other_worker_started = threading.Event()

    def worker(item: int) -> int:
        if item == 1:
            other_worker_started.wait(timeout=1)
            raise SystemExit("stop detail pass")
        other_worker_started.set()
        release.wait(timeout=2)
        return item

    with pytest.raises(SystemExit, match="stop detail pass"):
        runner.run(
            [1, 2],
            worker=worker,
            max_workers=2,
            timeout_seconds=1,
        )

    assert runner.is_draining() is True
    release.set()
    deadline = time.time() + 2
    while runner.is_draining() and time.time() < deadline:
        time.sleep(0.01)
    assert runner.is_draining() is False
