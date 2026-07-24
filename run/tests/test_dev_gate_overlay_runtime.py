"""精简后的 Overlay runtime 开发门禁。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]


def test_overlay_host_import_keeps_lcu_http_transport_lazy() -> None:
    run_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hextech.interfaces.overlay.host; print('requests' in sys.modules)",
        ],
        check=True,
        cwd=run_root,
        env={**__import__('os').environ, "PYTHONPATH": str(run_root / "src")},
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "False"


def test_template_runtime_is_split_and_uses_float16_matrix_cache() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "hextech" / "infrastructure" / "vision"
    modules = (
        root / "template_build.py",
        root / "template_cache.py",
        root / "template_diagnostics.py",
        root / "template_models.py",
        root / "template_runtime.py",
    )

    assert all(len(path.read_text(encoding="utf-8").splitlines()) <= 800 for path in modules)
    cache_text = (root / "template_cache.py").read_text(encoding="utf-8")
    assert "float16" in cache_text
    assert "tuple(float" not in cache_text
