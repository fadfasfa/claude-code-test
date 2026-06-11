"""桌面伴生兼容入口。

保留根目录启动方式不变，并按参数懒加载对应实现。
"""


def main() -> None:
    import sys

    if "--web-server" in sys.argv:
        from display.web_server import run_web_server

        run_web_server()
    elif "--game-overlay" in sys.argv:
        from game_overlay_host import main as run_overlay_main

        run_overlay_main([arg for arg in sys.argv[1:] if arg != "--game-overlay"])
    else:
        from display.hextech_ui import run_desktop

        run_desktop()


if __name__ == "__main__":
    main()
