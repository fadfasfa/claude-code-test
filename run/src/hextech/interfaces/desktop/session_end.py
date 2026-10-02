"""Windows session-end bridge；非 Windows 环境保持无操作。"""

from __future__ import annotations

import sys
from typing import Callable


def install_windows_session_end_bridge(root, *, on_query: Callable[[], object], on_end: Callable[[bool], object]):
    if sys.platform != "win32":
        return None
    from .client_layer import desktop_wrapper_hwnd, install_session_end_handler

    hwnd = desktop_wrapper_hwnd(root)
    install_session_end_handler(hwnd, on_query=on_query, on_end=on_end)
    return hwnd


__all__ = ["install_windows_session_end_bridge"]
