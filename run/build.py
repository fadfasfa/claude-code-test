"""打包入口薄壳。

根目录只保留一个稳定发布入口，具体打包流程由 `tools.build_package` 承担。

调用方: 命令行入口; 关键依赖: support.python_runtime、build_package。
"""

from hextech.support.python_runtime import PACKAGING_RUNTIME_PACKAGES, ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(require_packages=PACKAGING_RUNTIME_PACKAGES)

from tools.build_package import main


if __name__ == "__main__":
    main()
