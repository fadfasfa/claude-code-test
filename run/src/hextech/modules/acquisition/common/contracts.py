"""抓取模块使用的稳定契约重导出面。

权威定义位于 ``hextech.contracts``，本模块只保持 acquisition 内部导入简洁；
它不是旧 schema 兼容层。
"""

from hextech.contracts.data_pipeline import (
    FetchAttempt,
    ItemOutcome,
    ItemState,
    SourceRunManifestV2,
    utc_now_iso,
)

SourceRunManifest = SourceRunManifestV2

__all__ = ["FetchAttempt", "ItemOutcome", "ItemState", "SourceRunManifest", "SourceRunManifestV2", "utc_now_iso"]
