"""海克斯目录入口。

海克斯闭集、图标 manifest 和本地目录查找仍由 scraping 数据链路生成；catalog 层通过本模块
暴露只读目录能力，避免 UI 直接依赖抓取包路径。
"""

from __future__ import annotations

from hextech.scraping.augment_catalog import *  # noqa: F403
