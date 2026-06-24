"""Hextech 伴生系统主应用包。

本包是 run/ 重构后的稳定 import 根。业务实现已逐步收口到 core、catalog、
display、overlay、scraping 和 support。
"""

from __future__ import annotations

__all__ = ["core", "catalog", "display", "overlay", "scraping", "support"]
