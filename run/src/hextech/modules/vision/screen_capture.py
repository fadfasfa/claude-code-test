"""共享的 MSS 物理屏幕局部捕获后端。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

import mss
from mss.exception import ScreenShotError
from PIL import Image


logger = logging.getLogger(__name__)


class MssCaptureBackend:
    """在调用线程中懒建、复用并显式关闭一个 MSS 会话。"""

    def __init__(self, *, factory: Callable[[], Any] | None = None) -> None:
        self._factory = factory or mss.MSS
        self._session: Any | None = None

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = self._factory()
        return self._session

    def capture_rgb(self, rect: tuple[int, int, int, int]) -> Image.Image | None:
        left, top, right, bottom = (int(value) for value in rect)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
        monitor = {"left": left, "top": top, "width": width, "height": height}
        try:
            shot = self._get_session().grab(monitor)
            if tuple(int(value) for value in shot.size) != (width, height):
                return None
            return Image.frombytes("RGB", (width, height), shot.bgra, "raw", "BGRX")
        except (OSError, ScreenShotError, TypeError, ValueError):
            return None

    def close(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.close()
        except Exception:
            logger.debug("关闭 MSS 捕获会话失败。", exc_info=True)


__all__ = ["MssCaptureBackend"]
