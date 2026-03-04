import asyncio
import json
import logging
import random
from pathlib import Path
from playwright.async_api import async_playwright

# [Relative-Path-Standard] 自动锚定当前脚本所在目�?
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 自动创建数据目录
DATA_DIR.mkdir(parents=True, exist_ok=True)

# [Win-Encoding-Safe] 日志配置，使用相对路�?
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
        logger.info("[START] Launching Chromium (Portable Mode)...")
        
        # 注入真实环境指纹
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 目标：小黑盒《三角洲行动》百科页
        target_url = "https://www.xiaoheihe.cn/wiki/203"
        
        try:
            logger.info(f"[NAVIGATE] Loading {target_url}...")
            await page.goto(target_url, wait_until="networkidle", timeout=30000)

            # [Scraper-Ninja] 等待数据渲染
            logger.info("[WAIT] Waiting for JS rendering...")
            await page.wait_for_timeout(3000)

            # 执行 DOM 提取逻辑
            content_data = await page.evaluate("""() => {
                const titles = Array.from(document.querySelectorAll('.wiki-module-title')).map(el => el.innerText);
                const items = Array.from(document.querySelectorAll('.wiki-item-name')).map(el => el.innerText);
                return { titles, items };
            }""")

            if content_data['titles'] or content_data['items']:
                logger.info(f"[SUCCESS] Extracted {len(content_data['titles'])} modules and {len(content_data['items'])} items.")
                
                output = {
                    "timestamp": str(asyncio.get_event_loop().time()),
                    "url": target_url,
                    "payload": content_data
                }
                
                # [Relative-Path-Standard] 写入数据文件
                output_file = DATA_DIR / "heybox_wiki_browser.json"
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(output, f, indent=2, ensure_ascii=False)
                logger.info(f"[SUCCESS] Data saved to: {output_file.relative_to(BASE_DIR)}")
            else:
                logger.warning("[WARN] No structural data found. Capturing diagnostic screenshot...")
                screenshot_path = DATA_DIR / "debug_screenshot.png"
                await page.screenshot(path=str(screenshot_path))
                logger.info(f"[DEBUG] Screenshot saved to: {screenshot_path.relative_to(BASE_DIR)}")

        except Exception as e:
            logger.error(f"[FATAL] Runtime Error: {str(e)}")
        finally:
            if "browser" in locals():
                await browser.close()
            logger.info("[END] Browser session closed.")

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("Heybox Portable Browser Scraper V1.0")
    logger.info(f"Execution Root: {BASE_DIR}")
    logger.info("="*50)
    asyncio.run(run_scraper())
