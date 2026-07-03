from __future__ import annotations

"""Excel 采集统一入口。

调用 import_excel.py，捕获其 stdout（JSON 导入结果）落盘到 excel_import_report.json，
供 candidate-status / diff_summary 读取导入信号。退出码透传子进程，保持流水线契约。
"""

import json
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import EXCEL_IMPORT_REPORT_FILE, ensure_directories

CURRENT_DIR = Path(__file__).resolve().parent
IMPORT_EXCEL = CURRENT_DIR / "import_excel.py"


def _write_report(payload: dict, *, status: str) -> None:
    ensure_directories()
    report = {**payload, "status": status, "generated_at": datetime.now(UTC).isoformat()}
    EXCEL_IMPORT_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXCEL_IMPORT_REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    completed = subprocess.run(
        [sys.executable, str(IMPORT_EXCEL)],
        cwd=CURRENT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    exit_code = int(completed.returncode)
    stdout = (completed.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        _write_report(
            {"stderr": completed.stderr or "", "stdout": stdout},
            status="parse_failed",
        )
        return exit_code or 1
    if completed.stderr:
        payload["stderr"] = completed.stderr
    _write_report(payload, status="ok" if exit_code == 0 else "failed")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
