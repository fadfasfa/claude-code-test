"""打包入口薄壳。

根目录只保留一个稳定发布入口，具体打包流程由 `tooling.build.package` 承担。

调用方: 命令行入口; 关键依赖: modules.session.python_environment、build_package。
"""

from pathlib import Path
import sys

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.session.python_environment import (  # noqa: E402
    PACKAGING_RUNTIME_PACKAGES,
    ensure_python_311_for_source,
)


if __name__ == "__main__":
    ensure_python_311_for_source(
        module_name="tooling.build",
        require_packages=PACKAGING_RUNTIME_PACKAGES,
    )

from tooling.build.package import main  # noqa: E402


if __name__ == "__main__":
    main()
