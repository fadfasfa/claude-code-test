"""Web 服务 composition root。

调用方: 命令行入口; 关键依赖: support.python_runtime、support.log_utils、display.web.app。
"""

from hextech.modules.session.python_environment import ensure_python_311_for_source
from hextech.infrastructure.observability.logging import install_runtime_logging
from hextech.modules.data.ports.paths import ensure_var_layout


if __name__ == "__main__":
    ensure_python_311_for_source()

from hextech.interfaces.web.backend.app import run_web_server


def main() -> None:
    ensure_var_layout()
    install_runtime_logging()
    run_web_server()


if __name__ == "__main__":
    main()
