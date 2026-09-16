"""逐英雄请求领取器：优先级变化只影响尚未领取的任务。

在途请求有界，不因进入游戏而取消；取消仅由退出/来源拒绝等显式信号发起。
网络、落盘和进度发布均由调用方提供，本模块不创建另一套常驻服务。
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Event
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
STATIC_DOWNLOAD_CONCURRENCY = 4


@dataclass(frozen=True)
class DownloadContext:
    champion_id: str = ""
    in_game: bool = False
    pause_background: bool = False


class ChampionDownloads(Generic[T]):
    def __init__(self, context: Callable[[], DownloadContext], *, concurrency: int = STATIC_DOWNLOAD_CONCURRENCY):
        if not 1 <= concurrency <= STATIC_DOWNLOAD_CONCURRENCY:
            raise ValueError("static download concurrency must be in 1..4")
        self.context = context
        self.concurrency = concurrency

    def run(self, champion_ids: list[str], fetch: Callable[[str], T], *, stop: Event,
            completed: Callable[[str, T], None] | None = None) -> dict[str, T]:
        pending = list(dict.fromkeys(champion_ids))
        results: dict[str, T] = {}
        active: dict[Future[T], str] = {}
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="champion-download") as pool:
            while pending or active:
                context = self.context()
                limit = 1 if context.in_game else self.concurrency
                while pending and len(active) < limit and not stop.is_set():
                    priority = context.champion_id
                    if priority in pending:
                        champion_id = priority
                    elif context.pause_background:
                        break
                    else:
                        champion_id = pending[0]
                    pending.remove(champion_id)
                    active[pool.submit(fetch, champion_id)] = champion_id
                if not active:
                    if stop.is_set():
                        break
                    stop.wait(0.05)
                    continue
                done, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    champion_id = active.pop(future)
                    value = future.result()
                    results[champion_id] = value
                    if completed is not None:
                        completed(champion_id, value)
        return results
