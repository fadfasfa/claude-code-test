"""Scrapling 冒烟验证脚本。

只验证默认同步 HTTP 模式，避免把 browser mode 的浏览器安装
变成普通工具层验收前置。
用法：cd run && .venv\\Scripts\\python.exe -m hextech.infrastructure.transport.smoke_scrapling

调用方: 见 import 此模块的代码; 关键依赖: support.python_runtime、scraping.transport.scrapling_client。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[5]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.session.python_environment import ensure_python_311_for_source  # noqa: E402


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.infrastructure.transport.smoke_scrapling")


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def check_imports() -> None:
    """段 1：离线依赖检查，无需浏览器。"""
    if importlib.util.find_spec("scrapling") is None:
        print("[FAIL] scrapling 未安装")
        print("请执行：")
        print(r"  .\.venv\Scripts\python.exe -m pip install -r tooling\requirements\runtime.txt")
        sys.exit(1)

    print("[OK] scrapling 可 import")


def smoke_fetch() -> None:
    """段 2：在线抓取 example.com，验证 HTML 与状态码契约。"""
    from hextech.infrastructure.transport.scrapling_client import fetch_page

    print("正在抓取 https://example.com ...")
    result = fetch_page("https://example.com", css_selector="h1::text")

    if result.error:
        print(f"[FAIL] 抓取失败：{result.error}")
        sys.exit(1)

    if result.status_code != 200:
        print(f"[FAIL] status_code={result.status_code}，期望 200")
        sys.exit(1)

    if not result.html or "Example Domain" not in result.html:
        sample = str(result.html)[:200]
        print(f"[FAIL] 返回 HTML 不符合预期，前 200 字：{sample!r}")
        sys.exit(1)

    if result.extracted != ["Example Domain"]:
        print(f"[FAIL] selector 结果={result.extracted!r}，期望 ['Example Domain']")
        sys.exit(1)

    print(f"[OK] 抓取成功，html 长度={len(result.html)}，status={result.status_code}")


if __name__ == "__main__":
    check_imports()
    smoke_fetch()
