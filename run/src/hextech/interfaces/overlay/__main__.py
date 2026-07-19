"""拒绝绕过正式 composition root 启动 Overlay。

``hextech-overlay`` 和打包入口均指向 ``hextech.bootstrap.overlay``，那里会完成
LCU scanner、日志和运行目录的依赖装配。保留本文件只是为了给旧调用者提供明确
迁移提示，不能在 interface 层复制或隐式反向依赖 bootstrap。
"""

from __future__ import annotations


def main() -> int:
    print("请使用 hextech-overlay；该入口会从正式 composition root 启动。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
