"""测试 Hextech detail pass 的超时收口与防重叠行为。"""

from __future__ import annotations

import threading
import time


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
