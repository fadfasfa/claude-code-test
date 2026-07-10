"""Hextech detail pass 的并发执行边界。

超时后不能终止已经进入 Python 线程的请求，因此这里立即取消未开始任务，
并由后台 drain 线程等待剩余 worker 收口。在 drain 完成前拒绝新一轮执行，
避免两轮刷新同时写入共享状态。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, TypeVar


ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class DetailPassOutcome(Generic[ItemT, ResultT]):
    status: str
    results: list[tuple[ItemT, ResultT]] = field(default_factory=list)
    errors: list[tuple[ItemT, Exception]] = field(default_factory=list)
    pending_items: list[ItemT] = field(default_factory=list)


class DetailPassRunner:
    """确保同一进程内任意时刻最多只有一轮 detail worker 存活。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_generation = 0
        self._draining = False

    def is_draining(self) -> bool:
        with self._lock:
            return self._draining

    def run(
        self,
        items: Iterable[ItemT],
        *,
        worker: Callable[[ItemT], ResultT],
        max_workers: int,
        timeout_seconds: float,
        stop_event: threading.Event | None = None,
    ) -> DetailPassOutcome[ItemT, ResultT]:
        materialized = list(items)
        with self._lock:
            if self._draining:
                return DetailPassOutcome(status="draining", pending_items=materialized)
            self._active_generation += 1
            generation = self._active_generation
            self._draining = True

        executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        future_to_item: dict[Future[ResultT], ItemT] = {executor.submit(worker, item): item for item in materialized}
        results: list[tuple[ItemT, ResultT]] = []
        errors: list[tuple[ItemT, Exception]] = []
        drain_started = False

        def start_drain(status: str) -> DetailPassOutcome[ItemT, ResultT]:
            nonlocal drain_started
            outcome = self._start_drain(
                executor,
                future_to_item,
                generation=generation,
                status=status,
                results=results,
                errors=errors,
            )
            drain_started = True
            return outcome

        try:
            try:
                for future in as_completed(future_to_item, timeout=max(0.001, float(timeout_seconds))):
                    item = future_to_item[future]
                    if stop_event is not None and stop_event.is_set():
                        return start_drain("stopped")
                    try:
                        results.append((item, future.result()))
                    except Exception as exc:
                        errors.append((item, exc))
            except TimeoutError:
                return start_drain("timed_out")
        except BaseException:
            start_drain("failed")
            raise
        finally:
            if not drain_started:
                executor.shutdown(wait=True, cancel_futures=False)
                self._finish_generation(generation)

        return DetailPassOutcome(status="completed", results=results, errors=errors)

    def _start_drain(
        self,
        executor: ThreadPoolExecutor,
        future_to_item: dict[Future[ResultT], ItemT],
        *,
        generation: int,
        status: str,
        results: list[tuple[ItemT, ResultT]],
        errors: list[tuple[ItemT, Exception]],
    ) -> DetailPassOutcome[ItemT, ResultT]:
        pending_items: list[ItemT] = []
        for future, item in future_to_item.items():
            if future.done():
                continue
            pending_items.append(item)
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

        def drain() -> None:
            executor.shutdown(wait=True, cancel_futures=True)
            self._finish_generation(generation)

        threading.Thread(
            target=drain,
            name="hextech-detail-pass-drain",
            daemon=True,
        ).start()
        return DetailPassOutcome(
            status=status,
            results=list(results),
            errors=list(errors),
            pending_items=pending_items,
        )

    def _finish_generation(self, generation: int) -> None:
        with self._lock:
            if self._active_generation == generation:
                self._draining = False


DETAIL_PASS_RUNNER = DetailPassRunner()
