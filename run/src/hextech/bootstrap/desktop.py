"""桌面进程 composition root。

冻结单文件仍以参数区分受 supervisor 管理的子进程；源码态 CLI 则由
``pyproject.toml`` 的独立入口直接进入对应 bootstrap。

调用方: 命令行入口; 关键依赖: support.python_runtime、support.log_utils、display.web.app。
"""

import sys

sys.dont_write_bytecode = True

from hextech.modules.session.python_environment import ensure_python_311_for_source  # noqa: E402


if __name__ == "__main__":
    ensure_python_311_for_source()


def main() -> None:
    from hextech.infrastructure.lcu.official_overlay import scan_lcu_process
    from hextech.infrastructure.observability.logging import install_runtime_logging
    from hextech.interfaces.overlay.gameflow import configure_lcu_scanner
    from hextech.modules.data.ports.paths import ensure_var_layout

    ensure_var_layout()
    install_runtime_logging()
    configure_lcu_scanner(scan_lcu_process)
    if "--web-server" in sys.argv:
        from hextech.interfaces.web.backend.app import run_web_server

        run_web_server()
    elif "--runtime-supervisor" in sys.argv:
        from hextech.bootstrap.supervisor import main as run_runtime_supervisor

        args = [arg for arg in sys.argv[1:] if arg != "--runtime-supervisor"]
        raise SystemExit(run_runtime_supervisor(args))
    elif "--data-service" in sys.argv:
        from hextech.bootstrap.data_service_runtime import main as run_data_service

        args = [arg for arg in sys.argv[1:] if arg != "--data-service"]
        raise SystemExit(run_data_service(args))
    elif "--game-overlay" in sys.argv:
        from hextech.interfaces.overlay.host import main as run_overlay_main

        args = [arg for arg in sys.argv[1:] if arg != "--game-overlay"]
        raise SystemExit(run_overlay_main(args))
    elif "--overlay-sidecar" in sys.argv:
        from hextech.infrastructure.vision.sidecar import main as run_overlay_sidecar

        args = [arg for arg in sys.argv[1:] if arg != "--overlay-sidecar"]
        raise SystemExit(run_overlay_sidecar(args))
    else:
        from hextech.interfaces.desktop.app import run_desktop

        run_desktop()


if __name__ == "__main__":
    main()
