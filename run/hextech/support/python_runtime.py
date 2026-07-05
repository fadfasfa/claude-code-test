"""run 源码态 Python 运行时守卫。

本模块只负责源码态入口的解释器边界：项目源码与打包入口必须运行在
``run/.venv`` 的 Python 3.11 中。冻结态 exe 不走这里，避免影响
PyInstaller 产物。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path


REQUIRED_PYTHON = (3, 11)
REQUIRED_PYTHON_LABEL = "3.11"
PYTHON311_ENV = "HEXTECH_PYTHON311"
RUN_DIR = Path(__file__).resolve().parents[2]
DEFAULT_VENV_DIR = RUN_DIR / ".venv"

REQUIRED_RUNTIME_PACKAGES = ("scrapling", "cloakbrowser", "curl_cffi", "requests", "certifi")
PACKAGING_RUNTIME_PACKAGES = (*REQUIRED_RUNTIME_PACKAGES, "PyInstaller")
_IMPORT_MODULE_BY_PACKAGE = {
    "PyInstaller": "PyInstaller",
}


def _configure_console_encoding() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def current_python_version() -> tuple[int, int]:
    return int(sys.version_info[0]), int(sys.version_info[1])


def is_required_python() -> bool:
    return current_python_version() == REQUIRED_PYTHON


def default_venv_python_path() -> Path:
    if os.name == "nt":
        return DEFAULT_VENV_DIR / "Scripts" / "python.exe"
    return DEFAULT_VENV_DIR / "bin" / "python"


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os.path.abspath(os.fspath(left)).lower() == os.path.abspath(os.fspath(right)).lower()


def _path_command(raw_path: str | os.PathLike[str] | None) -> list[str] | None:
    value = str(raw_path or "").strip().strip('"')
    if not value:
        return None
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        return [str(path)]
    return None


def _dedupe_commands(commands: Iterable[Sequence[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for command in commands:
        parts = [str(part) for part in command if str(part).strip()]
        if not parts:
            continue
        key = tuple(part.lower() for part in parts)
        if key in seen:
            continue
        seen.add(key)
        result.append(parts)
    return result


def is_default_venv_python_command(command_prefix: Sequence[str]) -> bool:
    return len(command_prefix) == 1 and _same_path(command_prefix[0], default_venv_python_path())


def is_current_default_venv_python() -> bool:
    return _same_path(sys.executable, default_venv_python_path())


def is_current_explicit_python(env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    explicit = _path_command(environment.get(PYTHON311_ENV))
    return bool(explicit and _same_path(sys.executable, explicit[0]))


def iter_python_311_candidates(env: Mapping[str, str] | None = None) -> list[list[str]]:
    """返回允许的源码态候选解释器。

    默认只接受 ``run/.venv``。``HEXTECH_PYTHON311`` 是显式人工覆盖，用于
    临时诊断或非标准 venv；这里不再自动回退裸 ``py -3.11``。
    """

    environment = os.environ if env is None else env
    candidates: list[list[str]] = []

    venv_python = default_venv_python_path()
    if venv_python.exists():
        candidates.append([str(venv_python)])

    env_candidate = _path_command(environment.get(PYTHON311_ENV))
    if env_candidate:
        candidates.append(env_candidate)

    return _dedupe_commands(candidates)


def probe_python_version(command_prefix: Sequence[str], *, timeout_seconds: float = 5.0) -> tuple[int, int] | None:
    """验证候选解释器，返回其 major/minor；探测失败则视为不可用。"""

    code = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    try:
        result = subprocess.run(
            [*command_prefix, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip().splitlines()
    if not output:
        return None
    try:
        major, minor = output[-1].strip().split(".", 1)
        return int(major), int(minor)
    except (ValueError, TypeError):
        return None


def probe_python_imports(
    command_prefix: Sequence[str],
    packages: Sequence[str],
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, dict[str, str | bool]]:
    """检查候选解释器是否能导入关键包，并尽量返回版本号。"""

    package_names = [str(package) for package in packages if str(package).strip()]
    if not package_names:
        return {}

    code = (
        "import importlib.metadata as metadata, importlib.util, json\n"
        f"packages = {json.dumps(package_names)}\n"
        f"module_by_package = {json.dumps(_IMPORT_MODULE_BY_PACKAGE)}\n"
        "result = {}\n"
        "for package in packages:\n"
        "    module_name = module_by_package.get(package, package)\n"
        "    ok = importlib.util.find_spec(module_name) is not None\n"
        "    version = ''\n"
        "    if ok:\n"
        "        try:\n"
        "            version = metadata.version(package)\n"
        "        except metadata.PackageNotFoundError:\n"
        "            try:\n"
        "                version = metadata.version(package.lower())\n"
        "            except metadata.PackageNotFoundError:\n"
        "                version = ''\n"
        "    result[package] = {'ok': ok, 'version': version}\n"
        "print(json.dumps(result, ensure_ascii=False))\n"
    )
    try:
        result = subprocess.run(
            [*command_prefix, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return {package: {"ok": False, "version": ""} for package in package_names}
    if result.returncode != 0:
        return {package: {"ok": False, "version": ""} for package in package_names}
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {package: {"ok": False, "version": ""} for package in package_names}
    return {
        package: {
            "ok": bool((payload.get(package) or {}).get("ok")),
            "version": str((payload.get(package) or {}).get("version") or ""),
        }
        for package in package_names
    }


def missing_required_imports(command_prefix: Sequence[str], packages: Sequence[str]) -> list[str]:
    status = probe_python_imports(command_prefix, packages)
    return [package for package, item in status.items() if not item.get("ok")]


def _current_python_is_allowed(env: Mapping[str, str] | None = None) -> bool:
    if not is_required_python():
        return False
    return is_current_default_venv_python() or is_current_explicit_python(env)


def source_runtime_needs_switch(
    *,
    env: Mapping[str, str] | None = None,
    require_packages: Sequence[str] = REQUIRED_RUNTIME_PACKAGES,
) -> bool:
    """冻结态由打包器负责运行时；源码态必须落到稳定 venv。"""

    if getattr(sys, "frozen", False):
        return False
    if not _current_python_is_allowed(env):
        return True
    return bool(missing_required_imports([sys.executable], require_packages))


def find_python_311_command(
    env: Mapping[str, str] | None = None,
    *,
    require_packages: Sequence[str] = REQUIRED_RUNTIME_PACKAGES,
) -> list[str] | None:
    for command in iter_python_311_candidates(env=env):
        if probe_python_version(command) != REQUIRED_PYTHON:
            continue
        if missing_required_imports(command, require_packages):
            continue
        return command
    return None


def _creator_candidates() -> list[list[str]]:
    if os.name == "nt":
        return [["py", "-3.11"]]
    return [["python3.11"]]


def _find_python_311_creator() -> list[str] | None:
    for command in _creator_candidates():
        if probe_python_version(command) == REQUIRED_PYTHON:
            return command
    return None


def _run_bootstrap_command(command: Sequence[str], *, action: str) -> None:
    print(f"run/.venv 自动配置: {action}: {_format_command(command)}", file=sys.stderr)
    completed = subprocess.run(list(command), cwd=str(RUN_DIR), check=False)
    if completed.returncode != 0:
        raise SystemExit(
            "\n".join(
                [
                    f"run/.venv 自动配置失败：{action}",
                    f"失败命令：{_format_command(command)}",
                    f"退出码：{completed.returncode}",
                    "可手动修复：",
                    _setup_commands_text(),
                ]
            )
        )


def bootstrap_default_venv(*, require_packages: Sequence[str] = REQUIRED_RUNTIME_PACKAGES) -> list[str] | None:
    """首次源码态启动时创建/修复默认 run/.venv，并返回可执行 Python 命令。"""

    venv_python = default_venv_python_path()
    if not venv_python.exists():
        creator = _find_python_311_creator()
        if creator is None:
            return None
        _run_bootstrap_command(
            [*creator, "-m", "venv", str(DEFAULT_VENV_DIR)],
            action="创建 Python 3.11 虚拟环境",
        )

    if probe_python_version([str(venv_python)]) != REQUIRED_PYTHON:
        return None

    missing = missing_required_imports([str(venv_python)], require_packages)
    if missing:
        requirements = RUN_DIR / "requirements.txt"
        if not requirements.exists():
            raise SystemExit(f"缺少 requirements.txt：{requirements}")
        _run_bootstrap_command(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            action="升级 pip",
        )
        _run_bootstrap_command(
            [str(venv_python), "-m", "pip", "install", "-r", str(requirements)],
            action="安装 requirements.txt 依赖",
        )
        missing = missing_required_imports([str(venv_python)], require_packages)
        if missing:
            raise SystemExit(
                "\n".join(
                    [
                        f"run/.venv 自动配置后仍缺少依赖：{', '.join(missing)}",
                        "可手动修复：",
                        _setup_commands_text(),
                    ]
                )
            )
    return [str(venv_python)]


def build_reexec_command(
    python_command: Sequence[str],
    *,
    module_name: str | None = None,
    argv: Sequence[str] | None = None,
) -> list[str]:
    """重建当前入口命令；模块入口保留 ``-m`` 语义。"""

    current_argv = list(sys.argv if argv is None else argv)
    if module_name:
        return [*python_command, "-m", module_name, *current_argv[1:]]
    return [*python_command, *current_argv]


def _format_command(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def _setup_commands_text() -> str:
    venv_python = default_venv_python_path()
    if os.name == "nt":
        return (
            f"  cd /d {RUN_DIR}\n"
            "  py -3.11 -m venv .venv\n"
            r"  .\.venv\Scripts\python.exe -m pip install --upgrade pip" "\n"
            r"  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
        )
    return (
        f"  cd {RUN_DIR}\n"
        "  python3.11 -m venv .venv\n"
        "  ./.venv/bin/python -m pip install --upgrade pip\n"
        "  ./.venv/bin/python -m pip install -r requirements.txt"
    )


def format_missing_python_311_message(
    *,
    require_packages: Sequence[str] = REQUIRED_RUNTIME_PACKAGES,
) -> str:
    venv_python = default_venv_python_path()
    current_label = f"{current_python_version()[0]}.{current_python_version()[1]}"
    parts = [
        "run 源码态只支持 run/.venv 内的 Python 3.11。",
        f"当前解释器：{sys.executable} ({current_label})",
        f"期望解释器：{venv_python}",
    ]
    if not venv_python.exists():
        parts.append("未找到 run/.venv，入口会尝试自动创建；若自动配置失败，请手动执行：")
        parts.append(_setup_commands_text())
    else:
        version = probe_python_version([str(venv_python)])
        missing = missing_required_imports([str(venv_python)], require_packages) if version == REQUIRED_PYTHON else list(require_packages)
        if version != REQUIRED_PYTHON:
            parts.append(f"run/.venv 不是 Python {REQUIRED_PYTHON_LABEL}，请重建：")
            parts.append(_setup_commands_text())
        elif missing:
            parts.append(f"run/.venv 缺少依赖：{', '.join(missing)}")
            parts.append("请在 run 目录执行：")
            parts.append(r"  .\.venv\Scripts\python.exe -m pip install -r requirements.txt" if os.name == "nt" else "  ./.venv/bin/python -m pip install -r requirements.txt")
    parts.append(f"如需临时使用非默认 venv，可显式设置 {PYTHON311_ENV}=<python3.11.exe>。")
    return "\n".join(parts)


def ensure_python_311_for_source(
    *,
    module_name: str | None = None,
    argv: Sequence[str] | None = None,
    require_packages: Sequence[str] = REQUIRED_RUNTIME_PACKAGES,
) -> None:
    """入口最早调用；需要切换时以稳定 venv 重启并透传退出码。"""

    if not getattr(sys, "frozen", False):
        _configure_console_encoding()

    if not source_runtime_needs_switch(require_packages=require_packages):
        return

    python_command = find_python_311_command(require_packages=require_packages)
    if not python_command:
        python_command = bootstrap_default_venv(require_packages=require_packages)
    if not python_command:
        raise SystemExit(format_missing_python_311_message(require_packages=require_packages))

    command = build_reexec_command(python_command, module_name=module_name, argv=argv)
    print(
        "run 源码态要求使用 run/.venv 的 Python 3.11；"
        f"当前为 {current_python_version()[0]}.{current_python_version()[1]}，"
        f"已切换到：{_format_command(python_command)}",
        file=sys.stderr,
    )
    raise SystemExit(subprocess.call(command))
