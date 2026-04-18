from __future__ import annotations

import subprocess
import sys
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent
IMPORT_EXCEL = CURRENT_DIR / "import_excel.py"


def main() -> int:
    completed = subprocess.run([sys.executable, str(IMPORT_EXCEL)], cwd=CURRENT_DIR, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
