"""协同 source 域入口。

当前 source 类型与 scraper 实现同源，保留本模块作为后续拆分的稳定入口。

调用方: 见 import 此模块的代码; 关键依赖: _compat。
"""

from __future__ import annotations

from hextech._compat import alias_module as _alias_module

_alias_module(__name__, "hextech.scraping.synergy.scraper")
