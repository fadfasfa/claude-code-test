import httpx
import asyncio
import json
import logging
import random
import time
from datetime import datetime

# [Win-Encoding-Safe] 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data/heybox_wiki.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("HeyboxProbe")

class WikiScraper:
    def __init__(self):
        self.base_url = "https://api.xiaoheihe.cn"
        self.ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ]
        self.headers = {
            "Referer": "https://www.xiaoheihe.cn/",
            "Origin": "https://www.xiaoheihe.cn",
            "x-client-type": "web",
            "Accept": "application/json, text/plain, */*"
        }
        self.cookies = {}

    async def _init_session(self, client):
        """[Scraper-Ninja] 捕获 Initial Cookies"""
        try:
            logger.info("[INIT] Requesting homepage to capture cookies...")
            resp = await client.get("https://www.xiaoheihe.cn/", timeout=10.0)
            self.cookies.update(resp.cookies)
            logger.info(f"[INIT] Captured {len(self.cookies)} cookies.")
        except Exception as e:
            logger.error(f"[INIT] Cookie capture failed: {str(e)}")

    async def probe_wiki(self):
        async with httpx.AsyncClient(headers=self.headers, follow_redirects=True) as client:
            await self._init_session(client)
            
            # 随机延迟防止指纹封禁
            delay = random.uniform(2, 4)
            logger.info(f"[WAIT] Sleeping for {delay:.2f}s...")
            await asyncio.sleep(delay)

            # 目标百科端点 (Wiki ID 203 探测)
            target_url = f"{self.base_url}/wiki/get_homepage_v3/"
            params = {
                "wiki_id": "203",
                "os_type": "web",
                "version": "999.0.0",
                "_t": int(time.time())
            }

            self.headers["User-Agent"] = random.choice(self.ua_pool)
            
            try:
                logger.info(f"[PROBE] Target: {target_url}")
                response = await client.get(target_url, params=params, cookies=self.cookies)
                response.raise_for_status()
                
                data = response.json()
                logger.info(f"[SUCCESS] Status: {data.get('status')}")
                
                # 持久化存储 [Python-Debugger] 
                with open('data/heybox_wiki_sample.json', 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                logger.info("[SUCCESS] Data saved to data/heybox_wiki_sample.json")

            except httpx.HTTPStatusError as e:
                logger.error(f"[ERROR] HTTP Error: {e.response.status_code}")
                logger.debug(f"[DEBUG] Headers: {e.response.headers}")
            except Exception as e:
                logger.error(f"[FATAL] Traceback: {str(e)}")

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("Heybox Wiki Probe System Starting")
    logger.info("="*50)
    asyncio.run(WikiScraper().probe_wiki())