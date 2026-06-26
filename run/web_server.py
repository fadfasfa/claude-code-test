"""Web 服务兼容入口。

保留根目录启动方式不变，并把实际实现委托给 `hextech.display.web.app`。
"""

from hextech.support.python_runtime import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source()

from hextech.display.web.app import app, run_web, run_web_server


if __name__ == "__main__":
    run_web_server()
