"""验证 pytest 及其子进程只能使用临时 HEXTECH_VAR_DIR。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_subprocess_inherits_pytest_runtime_root_not_real_run_var() -> None:
    run_root = Path(__file__).resolve().parents[1]
    parent_root = Path(os.environ["HEXTECH_VAR_DIR"]).resolve()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "root=Path(os.environ['HEXTECH_VAR_DIR']).resolve(); "
                "(root / 'subprocess-isolation-probe.txt').write_text('ok', encoding='utf-8'); "
                "print(root)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    child_root = Path(completed.stdout.strip()).resolve()

    assert child_root == parent_root
    assert child_root != (run_root / "var").resolve()
    assert (child_root / "subprocess-isolation-probe.txt").read_text(encoding="utf-8") == "ok"


def test_subprocess_keeps_supplied_pythonpath_after_project_paths(tmp_path: Path) -> None:
    run_root = Path(__file__).resolve().parents[1]
    supplied_path = tmp_path / "caller-pythonpath"
    completed = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['PYTHONPATH'])"],
        check=True,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(supplied_path)},
    )

    python_paths = completed.stdout.strip().split(os.pathsep)

    assert python_paths[:2] == [str(run_root / "src"), str(run_root)]
    assert str(supplied_path) in python_paths


def test_runtime_isolation_preserves_popen_class_for_fresh_asyncio_import() -> None:
    conftest = Path(__file__).with_name("conftest.py")
    code = """
import runpy
import subprocess
import sys
runtime = runpy.run_path(sys.argv[1])
try:
    assert isinstance(subprocess.Popen, type), 'Popen must remain subclassable'
    import asyncio
    if sys.platform == 'win32':
        from asyncio.windows_utils import Popen
        assert issubclass(Popen, subprocess.Popen)
    print('fresh-asyncio-ok')
finally:
    runtime['pytest_sessionfinish'](None, 0)
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(conftest)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fresh-asyncio-ok" in completed.stdout
