"""采集应用层使用的外部能力端口。

端口只描述远端读取与来源发布能力；HTTP、browser 和文件系统实现由
``hextech.infrastructure`` 提供，并在 bootstrap 中组装。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .contracts import FetchAttempt, SourceRunManifest


class TransportPort(Protocol):
    def fetch(self, url: str, *, timeout: float, browser: bool = False) -> tuple[FetchAttempt, bytes]: ...


class SourceRunStore(Protocol):
    def current_artifact(self, source: str) -> Path | None: ...

    def publish(
        self,
        manifest: SourceRunManifest,
        *,
        payload: bytes,
        report: Mapping[str, Any],
    ) -> Path: ...


__all__ = ["SourceRunStore", "TransportPort"]
