"""DataService composition root；唯一 generation 发布进程。"""

from __future__ import annotations

from hextech.infrastructure.observability.logging import install_runtime_logging
from hextech.modules.data.ports.paths import ensure_var_layout
from hextech.bootstrap.data_service_runtime import main as run_data_service


def main() -> int:
    ensure_var_layout()
    install_runtime_logging()
    return int(run_data_service(None) or 0)


__all__ = ["main"]
