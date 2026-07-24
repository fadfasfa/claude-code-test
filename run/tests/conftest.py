"""pytest 入口补齐 ``src`` 与项目根 import path。

这些测试从仓库根执行时也应能导入 `hextech` 和 `tooling`，避免把导入路径问题
误判成运行态失败。

调用方: pytest 自动发现; 关键依赖: 见 imports。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


RUN_DIR = Path(__file__).resolve().parents[1]
for candidate in (RUN_DIR / "src", RUN_DIR, RUN_DIR / "tests"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


# 必须在 pytest 收集测试模块前确定运行态根目录。`paths.py` 会在导入时缓存
# `HEXTECH_VAR_DIR`，因此仅用 fixture 在测试函数执行时设置已经太晚了。
PYTEST_HEXTECH_VAR_DIR = Path(tempfile.mkdtemp(prefix="hextech-pytest-var-")).resolve()
os.environ["HEXTECH_VAR_DIR"] = str(PYTEST_HEXTECH_VAR_DIR)


def _evict_runtime_path_modules() -> None:
    """清掉可能在 pytest 插件阶段提前加载的路径缓存。

    仅清理路径和运行态解析模块，不删除整个 `hextech` 包，避免破坏已经注册的
    pytest collector；子进程默认继承上面的环境变量，因此也会指向同一临时目录。
    """

    prefixes = (
        "hextech.modules.data.ports.paths",
        "hextech.modules.data.catalog.runtime_store",
        "hextech.modules.data.source_runs",
        "hextech.modules.data.generation",
    )
    for module_name in tuple(sys.modules):
        if module_name.startswith(prefixes):
            sys.modules.pop(module_name, None)


_evict_runtime_path_modules()


_ORIGINAL_POPEN = subprocess.Popen


def _isolated_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    """给测试子进程显式传递运行态根目录。

    默认 `Popen` 虽会继承环境，但测试经常传入裁剪过的 `env`。这里把当前环境
    展开后再覆盖调用方给出的变量，确保普通子进程不会意外回落到真实 `run/var`；
    专门验证自定义 runtime 根的测试仍可显式覆盖 `HEXTECH_VAR_DIR`。
    """

    env = dict(os.environ)
    supplied = kwargs.get("env")
    if supplied is not None:
        env.update({str(key): str(value) for key, value in dict(supplied).items()})
    env.setdefault("HEXTECH_VAR_DIR", str(PYTEST_HEXTECH_VAR_DIR))
    # pytest 的 sys.path 不会自动进入 ``python -c`` 子进程；显式追加本地 src
    # 才能同时验证子进程隔离和源码态导入，而不依赖真实 run/.venv 是否已安装。
    project_paths = [str(RUN_DIR / "src"), str(RUN_DIR)]
    inherited_paths = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*project_paths, *inherited_paths]))
    kwargs["env"] = env
    return _ORIGINAL_POPEN(*args, **kwargs)


subprocess.Popen = _isolated_popen  # type: ignore[assignment]


def pytest_sessionfinish(session, exitstatus) -> None:  # type: ignore[no-untyped-def]
    """保留失败现场到进程退出，随后只清理本次创建的临时目录。"""

    del session, exitstatus
    subprocess.Popen = _ORIGINAL_POPEN  # type: ignore[assignment]
    # 不在这里删除真实 `run/var`：该路径完全由本文件创建且有固定前缀。
    import shutil

    if PYTEST_HEXTECH_VAR_DIR.name.startswith("hextech-pytest-var-"):
        shutil.rmtree(PYTEST_HEXTECH_VAR_DIR, ignore_errors=True)
