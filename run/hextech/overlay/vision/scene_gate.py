"""overlay vision 场景门控入口。

调用方: 经别名转发至 overlay.vision.sidecar 的引用方; 关键依赖: hextech._compat。
"""

from __future__ import annotations

from hextech._compat import alias_module as _alias_module

_alias_module(__name__, "hextech.overlay.vision.sidecar")
