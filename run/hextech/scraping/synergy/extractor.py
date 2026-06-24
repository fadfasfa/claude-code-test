"""协同 extractor 域入口。

当前 extractor 类型与 scraper 实现同源，保留本模块作为后续拆分的稳定入口。
"""

from __future__ import annotations

from hextech._compat import alias_module as _alias_module

_alias_module(__name__, "hextech.scraping.synergy.scraper")
