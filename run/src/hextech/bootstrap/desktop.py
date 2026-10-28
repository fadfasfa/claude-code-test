"""桌面进程 composition root。

冻结单文件仍以参数区分受 App 管理的子进程；源码态 CLI 则由
``pyproject.toml`` 的独立入口直接进入对应 bootstrap。

调用方: 命令行入口; 关键依赖: support.python_runtime、support.log_utils、display.web.app。
"""

import sys

sys.dont_write_bytecode = True

from hextech.modules.session.python_environment import ensure_python_311_for_source  # noqa: E402


if __name__ == "__main__":
    ensure_python_311_for_source()


def _initialize_packaged_release() -> dict | None:
    """冻结App启动前显式初始化同包v4 release；源码态绝不触碰该路径。"""

    if not getattr(sys, "frozen", False):
        return
    from hextech.infrastructure.persistence.release_seed import install_v4_seed
    from hextech.modules.data.ports.paths import resource_path, get_var_dir
    return install_v4_seed(resource_path("seeds", "runtime.v4"), get_var_dir())


def _frozen_role(argv: list[str] | None = None) -> str:
    """在任何seed或日志重活之前解析冻结 EXE 角色。"""

    arguments = list(sys.argv[1:] if argv is None else argv)
    for role in (
        "--desktop-presentation-smoke",
        "--acquisition-worker",
        "--game-overlay",
        "--overlay-sidecar",
    ):
        if role in arguments:
            return role
    if arguments:
        raise SystemExit("unsupported runtime role or arguments")
    return "desktop"


def main() -> None:
    role = _frozen_role()
    try:
        _run(role)
    except Exception as exc:
        if role == "desktop":
            from hextech.interfaces.desktop.startup_failure import show_startup_failure

            show_startup_failure(exc)
            raise SystemExit(1) from exc
        raise


def _run(role: str) -> None:
    if role == "--desktop-presentation-smoke":
        from hextech.interfaces.desktop.presentation_smoke import run_desktop_presentation_smoke
        from hextech.modules.session.process_bootstrap import publish_process_bootstrap

        # Isolated GUI acceptance must precede normal runtime/LCU/seed initialization.
        try:
            result = run_desktop_presentation_smoke()
        except Exception as exc:
            publish_process_bootstrap({"state": "failed", "error_type": type(exc).__name__, "reason": str(exc)})
            raise SystemExit(1) from exc
        publish_process_bootstrap(result)
        raise SystemExit(0 if result.get("state") == "ok" else 1)

    from hextech.infrastructure.lcu.official_overlay import scan_lcu_process
    from hextech.infrastructure.observability.logging import install_runtime_logging
    from hextech.interfaces.overlay.gameflow import configure_lcu_scanner
    from hextech.modules.data.ports.paths import ensure_var_layout

    ensure_var_layout()
    install_runtime_logging()
    configure_lcu_scanner(scan_lcu_process)
    if role == "--acquisition-worker":
        from hextech.bootstrap.acquisition_worker import main as run_acquisition_worker

        args = [arg for arg in sys.argv[1:] if arg != "--acquisition-worker"]
        raise SystemExit(run_acquisition_worker(args))
    elif role == "--game-overlay":
        from hextech.interfaces.overlay.host import main as run_overlay_main

        args = [arg for arg in sys.argv[1:] if arg != "--game-overlay"]
        raise SystemExit(run_overlay_main(args))
    elif role == "--overlay-sidecar":
        from hextech.infrastructure.vision.sidecar import main as run_overlay_sidecar

        args = [arg for arg in sys.argv[1:] if arg != "--overlay-sidecar"]
        raise SystemExit(run_overlay_sidecar(args))
    else:
        from hextech.interfaces.desktop.app import run_desktop

        from hextech.bootstrap.data_service_runtime import create_embedded_data_service
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner
        run_desktop(seed_initializer=_initialize_packaged_release,
                    data_runtime_factory=create_embedded_data_service, runtime_owner_factory=AppRuntimeOwner)


if __name__ == "__main__":
    main()
