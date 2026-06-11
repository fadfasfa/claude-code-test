"""桌面伴生兼容入口。

保留根目录启动方式不变，并把实际实现委托给 `display.hextech_ui`。
"""

from display.hextech_ui import HextechUI, run_desktop


def main() -> None:
    import sys

    if "--web-server" in sys.argv:
        from display.web_server import run_web_server

        run_web_server()
    elif "--game-overlay" in sys.argv:
        from display.game_overlay_host import main as run_game_overlay

        args = [arg for arg in sys.argv[1:] if arg != "--game-overlay"]
        raise SystemExit(run_game_overlay(args))
    elif "--overlay-sidecar" in sys.argv:
        from processing.overlay_vision_sidecar import main as run_overlay_sidecar

        args = [arg for arg in sys.argv[1:] if arg != "--overlay-sidecar"]
        raise SystemExit(run_overlay_sidecar(args))
    else:
        run_desktop()


if __name__ == "__main__":
    main()
