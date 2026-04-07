from app.ui.launcher import HextechUI, run_desktop


def main() -> None:
    import sys

    if "--web-server" in sys.argv:
        from app.api.launcher import run_web_server

        run_web_server()
    else:
        run_desktop()


if __name__ == "__main__":
    main()
