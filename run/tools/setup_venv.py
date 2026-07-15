"""创建和验证 run/.venv 的显式修复工具。

源码入口会在默认 ``run/.venv`` 缺失时尝试自动创建并安装依赖；本工具保留
给人工修复、依赖重装或 smoke 检查使用。手动运行：
``py -3.11 tools/setup_venv.py``。

调用方: 见 import 此模块的代码; 关键依赖: support.python_runtime。
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from hextech.support.python_runtime import (  # noqa: E402
    PACKAGING_RUNTIME_PACKAGES,
    REQUIRED_PYTHON,
    default_venv_python_path,
    probe_python_imports,
    probe_python_version,
)


def _run(command: Sequence[str], *, cwd: Path = RUN_DIR) -> None:
    print(f"$ {' '.join(str(part) for part in command)}")
    completed = subprocess.run(list(command), cwd=str(cwd), check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _creator_candidates() -> list[list[str]]:
    if os.name == "nt":
        return [["py", "-3.11"]]
    return [["python3.11"]]


def _find_python_311_creator() -> list[str]:
    for command in _creator_candidates():
        if probe_python_version(command) == REQUIRED_PYTHON:
            return command
    raise SystemExit("未找到可用于创建 run/.venv 的 Python 3.11。Windows 请先确认 `py -3.11` 可用。")


def _install_dependencies(venv_python: Path) -> None:
    requirements = RUN_DIR / "tools" / "requirements" / "compat.txt"
    if not requirements.exists():
        raise SystemExit(f"缺少 tools/requirements/compat.txt：{requirements}")
    _run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    _run([str(venv_python), "-m", "pip", "install", "-r", str(requirements)])


def _run_scrapling_smoke(venv_python: Path) -> dict[str, object]:
    command = [str(venv_python), "-m", "hextech.scraping.transport.smoke_scrapling"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command,
        cwd=str(RUN_DIR),
        capture_output=True,
        env=env,
        check=False,
        timeout=45,
    )
    def decode_output(data: bytes | None) -> str:
        if not data:
            return ""
        encodings = ("utf-8", "gbk", locale.getpreferredencoding(False))
        for encoding in encodings:
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    output = "\n".join(part for part in (stdout, stderr) if part.strip())
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "output_tail": output[-1200:],
    }


def _environment_summary(venv_python: Path, *, include_smoke: bool) -> dict[str, object]:
    version = probe_python_version([str(venv_python)])
    package_status = probe_python_imports([str(venv_python)], PACKAGING_RUNTIME_PACKAGES)
    summary: dict[str, object] = {
        "venv_python": str(venv_python),
        "python_version": ".".join(str(part) for part in version) if version else "",
        "packages": package_status,
    }
    if include_smoke:
        summary["scrapling_smoke"] = _run_scrapling_smoke(venv_python)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="创建/修复 run/.venv 并输出环境摘要。")
    parser.add_argument("--check", action="store_true", help="只检查现有 run/.venv，不安装依赖。")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过 example.com Scrapling 在线冒烟。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    venv_python = default_venv_python_path()

    if not args.check:
        if not venv_python.exists():
            creator = _find_python_311_creator()
            _run([*creator, "-m", "venv", ".venv"])
        version = probe_python_version([str(venv_python)])
        if version != REQUIRED_PYTHON:
            raise SystemExit(
                f"run/.venv 不是 Python 3.11：{venv_python}。请移除 .venv 后重新运行本工具。"
            )
        _install_dependencies(venv_python)

    if not venv_python.exists():
        raise SystemExit(f"未找到 run/.venv 解释器：{venv_python}")
    if probe_python_version([str(venv_python)]) != REQUIRED_PYTHON:
        raise SystemExit(f"run/.venv 必须是 Python 3.11：{venv_python}")

    summary = _environment_summary(venv_python, include_smoke=not args.skip_smoke)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    smoke = summary.get("scrapling_smoke")
    if isinstance(smoke, dict) and not smoke.get("ok"):
        return 1
    packages = summary.get("packages")
    package_items = packages.items() if isinstance(packages, dict) else []
    missing = [
        name
        for name, status in package_items
        if not (status or {}).get("ok")
    ]
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
