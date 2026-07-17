"""Overlay host composition root。"""

from __future__ import annotations

from hextech.infrastructure.observability.logging import install_runtime_logging
from hextech.infrastructure.lcu.official_overlay import scan_lcu_process
from hextech.modules.data.ports.paths import ensure_var_layout
from hextech.interfaces.overlay.gameflow import configure_lcu_scanner
from hextech.interfaces.overlay.host import main as run_overlay


def main() -> int:
    ensure_var_layout()
    install_runtime_logging()
    configure_lcu_scanner(scan_lcu_process)
    return int(run_overlay(None) or 0)


__all__ = ["main"]
