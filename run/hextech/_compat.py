"""同包模块别名辅助函数。

少量细分目标模块仍复用同一实现模块。这里把别名模块名指向真实实现模块，避免复制模块级缓存和锁。
"""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType


def alias_module(new_name: str, old_name: str) -> ModuleType:
    """把当前模块名映射到同包内的真实实现模块对象。"""

    module = import_module(old_name)
    sys.modules[new_name] = module
    return module
