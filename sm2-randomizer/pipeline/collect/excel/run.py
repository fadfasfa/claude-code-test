from __future__ import annotations

"""Excel 采集统一入口。

负责调用 Excel 导入脚本并透传退出码，用于在统一流水线中稳定编排 Excel 刷新步骤。
"""

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
