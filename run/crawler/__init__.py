"""crawler 子包：基于 Crawl4AI 的独立网页抓取层。

与 run/scraping/ 平行，不依赖现有爬虫；供 CC/CX 直接调用。
"""

from crawler.crawl4ai_client import FetchResult, fetch_page

__all__ = ["fetch_page", "FetchResult"]
