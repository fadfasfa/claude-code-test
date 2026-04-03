import asyncio
import json
import logging
import random
from pathlib import Path
from playwright.async_api import async_playwright

# 脚本根目录。
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 确保数据目录存在。
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 日志输出到控制台和文件。
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / 'browser_scraper.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("HeyboxBrowser")

async def run_scraper():
    async with async_playwright() as p:
        logger.info("[启动] 正在启动 Chromium（便携模式）...")
        
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        target_url = "https://www.xiaoheihe.cn/wiki/203"
        
        try:
            logger.info(f"[导航] 正在加载 {target_url}...")
            await page.goto(target_url, wait_until="networkidle", timeout=30000)

            logger.info("[等待] 正在等待脚本渲染...")
            await page.wait_for_timeout(3000)

            content_data = await page.evaluate("""() => {
                const titles = Array.from(document.querySelectorAll('.wiki-module-title')).map(el => el.innerText);
                const items = Array.from(document.querySelectorAll('.wiki-item-name')).map(el => el.innerText);
                return { titles, items };
            }""")

            if content_data['titles'] or content_data['items']:
                logger.info(f"[成功] 已提取 {len(content_data['titles'])} 个模块和 {len(content_data['items'])} 个条目。")
                
                output = {
                    "timestamp": str(asyncio.get_event_loop().time()),
                    "url": target_url,
                    "payload": content_data
                }
                
                output_file = DATA_DIR / "heybox_wiki_browser.json"
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
                logger.info(f"[成功] 数据已保存到：{output_file.relative_to(BASE_DIR)}")
            else:
                logger.warning("[警告] 未找到结构数据，正在截取诊断截图...")
                screenshot_path = DATA_DIR / "debug_screenshot.png"
                await page.screenshot(path=str(screenshot_path))
                logger.info(f"[调试] 截图已保存到：{screenshot_path.relative_to(BASE_DIR)}")

        except Exception as e:
            logger.error(f"[致命] 运行时错误：{str(e)}")
        finally:
            if "browser" in locals():
                await browser.close()
            logger.info("[结束] 浏览器会话已关闭。")

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("小黑盒便携浏览器抓取器 V1.0")
    logger.info(f"执行根目录：{BASE_DIR}")
    logger.info("="*50)
    asyncio.run(run_scraper())
