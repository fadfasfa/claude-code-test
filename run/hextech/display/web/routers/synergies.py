"""Web synergies 路由域入口。

当前路由仍由统一 api 模块注册；本文件保留目标域名，便于后续拆 APIRouter。
"""

from __future__ import annotations

from hextech._compat import alias_module as _alias_module

_alias_module(__name__, "hextech.display.web.api")
