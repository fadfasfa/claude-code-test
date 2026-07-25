"""Windows 系统托盘控制器。

托盘线程只负责接收菜单事件，所有 Tk 和运行时状态变更都通过 ``root.after``
回到主线程，避免 pystray 的后台消息循环直接触碰 Tk。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PIL import Image, ImageDraw


logger = logging.getLogger(__name__)


def _build_tray_image() -> Image.Image:
    """绘制独立于外部资源的简洁 Hextech 托盘图标。"""

    image = Image.new("RGBA", (64, 64), (1, 10, 19, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((5, 5, 59, 59), outline=(200, 155, 60, 255), width=5)
    draw.line((19, 17, 19, 47), fill=(45, 212, 191, 255), width=7)
    draw.line((45, 17, 45, 47), fill=(45, 212, 191, 255), width=7)
    draw.line((19, 32, 45, 32), fill=(245, 248, 255, 255), width=6)
    return image


class DesktopTrayController:
    """封装 pystray 生命周期，并把所有动作转发给 Desktop。"""

    def __init__(
        self,
        *,
        dispatch: Callable[[Callable[[], None]], bool],
        show_callback: Callable[[], None],
        restart_recognition_callback: Callable[[], None],
        exit_callback: Callable[[], None],
        status_text: Callable[[], str],
    ) -> None:
        self._dispatch = dispatch
        self._show_callback = show_callback
        self._restart_recognition_callback = restart_recognition_callback
        self._exit_callback = exit_callback
        self._status_text = status_text
        self._icon = None

    def start(self) -> bool:
        if self._icon is not None:
            return True
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("显示 Hextech", self._show, default=True),
                pystray.MenuItem(lambda _item: self._status_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("重启识别", self._restart_recognition),
                pystray.MenuItem("退出 Hextech", self._exit),
            )
            self._icon = pystray.Icon("hextech-companion", _build_tray_image(), "Hextech 伴生终端", menu)
            self._icon.run_detached()
            return True
        except Exception:
            self._icon = None
            logger.exception("系统托盘启动失败。")
            return False

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            logger.debug("停止系统托盘失败。", exc_info=True)

    def refresh(self) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            logger.debug("刷新托盘菜单失败。", exc_info=True)

    def _show(self, _icon=None, _item=None) -> None:
        self._dispatch(self._show_callback)

    def _restart_recognition(self, _icon=None, _item=None) -> None:
        self._dispatch(self._restart_recognition_callback)

    def _exit(self, _icon=None, _item=None) -> None:
        self._dispatch(self._exit_callback)


__all__ = ["DesktopTrayController"]
