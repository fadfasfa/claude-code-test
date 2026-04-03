import httpx
import asyncio
import json
import logging
import random
import time
import os
from pathlib import Path
from datetime import datetime

# [Dynamic-Path-Standard] 计算项目根目录和数据路径
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# [Win-Encoding-Safe] 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / 'heybox_wiki.log', encoding='utf-8')
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
    # 捕获初始 Cookie
        try:
            logger.info("[初始化] 正在访问主页以捕获 Cookie...")
            resp = await client.get("https://www.xiaoheihe.cn/", timeout=10.0)
            self.cookies.update(resp.cookies)
            logger.info(f"[初始化] 已捕获 {len(self.cookies)} 个 Cookie。")
        except Exception as e:
            logger.error(f"[初始化] Cookie 获取失败：{str(e)}")

    async def probe_wiki(self):
        async with httpx.AsyncClient(headers=self.headers, follow_redirects=True) as client:
            await self._init_session(client)
            
            # 随机延迟防止指纹封禁
            delay = random.uniform(2, 4)
            logger.info(f"[等待] 正在休眠 {delay:.2f} 秒...")
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
                logger.info(f"[探测] 目标：{target_url}")
                response = await client.get(target_url, params=params, cookies=self.cookies)
                response.raise_for_status()
                
                data = response.json()
                logger.info(f"[成功] 状态：{data.get('status')}")

                # 持久化存储 [Python-Debugger]
                output_file = DATA_DIR / 'heybox_wiki_sample.json'
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                logger.info(f"[成功] 数据已保存到 {output_file}")

            except httpx.HTTPStatusError as e:
                logger.error(f"[错误] HTTP 错误：{e.response.status_code}")
                logger.debug(f"[调试] 响应头：{e.response.headers}")
            except Exception as e:
                logger.error(f"[致命] 错误堆栈：{str(e)}")

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("小黑盒百科探测程序启动")
    logger.info("="*50)
    asyncio.run(WikiScraper().probe_wiki())
