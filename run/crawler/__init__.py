"""crawler 子包：基于 Scrapling 的独立网页抓取层。

与 run/scraping/ 平行，不依赖现有业务爬虫；供 Codex 工具任务直接调用。
"""

from crawler.scrapling_client import FetchResult, fetch_page

__all__ = ["fetch_page", "FetchResult"]
