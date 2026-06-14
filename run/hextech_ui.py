"""桌面伴生兼容入口。

保留根目录启动方式不变，并按参数懒加载对应实现。
"""


def main() -> None:
    import sys

    if "--web-server" in sys.argv:
        from display.web_server import run_web_server

        run_web_server()
    elif "--game-overlay" in sys.argv:
        # PR 走根目录薄壳避免冻结态依赖源码 .py 路径；main 合入 sidecar CLI 入口同步保留。
        from game_overlay_host import main as run_overlay_main

        args = [arg for arg in sys.argv[1:] if arg != "--game-overlay"]
        raise SystemExit(run_overlay_main(args))
    elif "--overlay-sidecar" in sys.argv:
        from processing.overlay_vision_sidecar import main as run_overlay_sidecar

        args = [arg for arg in sys.argv[1:] if arg != "--overlay-sidecar"]
        raise SystemExit(run_overlay_sidecar(args))
    else:
        from display.hextech_ui import run_desktop

        run_desktop()


if __name__ == "__main__":
    main()
