# -*- coding: utf-8 -*-
"""
ApexLoL 信息爬虫 - Playwright 渲染层独立爬虫

针对 apexlol.info 的独立 Playwright 渲染层爬虫，实施降维打击获取底层数据。
绝对隔离于现有业务域。
"""

import logging
import os
import random
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# 常见 PC 浏览器 User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    """获取随机 User-Agent"""
    return random.choice(USER_AGENTS)


def anti_fingerprint_sleep(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    """
    防指纹动态休眠

    Args:
        min_seconds: 最小休眠秒数
        max_seconds: 最大休眠秒数
    """
    sleep_time = random.uniform(min_seconds, max_seconds)
    logger.info(f"执行防指纹休眠：{sleep_time:.2f} 秒")
    time.sleep(sleep_time)


class ApexSpider:
    """
    ApexLoL 独立爬虫类

    使用 Playwright 进行页面渲染，获取动态加载的数据。
    完全隔离于现有业务域，独立运行。
    """

    def __init__(self, headless: bool = True):
        """
        初始化爬虫

        Args:
            headless: 是否无头模式，默认 True
        """
        self.headless = headless
        self.base_url = "https://apexlol.info/zh"
        self.user_agent = get_random_user_agent()
        logger.info(f"ApexSpider 初始化完成，User-Agent: {self.user_agent[:50]}...")

    def fetch_page(self, url: str, page) -> str:
        """
        获取页面内容（带防指纹休眠）

        Args:
            url: 目标 URL
            page: Playwright page 对象

        Returns:
            页面 HTML 内容，失败返回 None
        """
        try:
            logger.info(f"正在加载页面：{url}")
            response = page.goto(url, wait_until="networkidle", timeout=30000)

            if response is None:
                logger.error(f"页面无响应：{url}")
                return None

            if response.status != 200:
                logger.error(f"页面返回状态码异常：{response.status}, URL: {url}")
                return None

            # 关键页面加载后执行防指纹休眠
            anti_fingerprint_sleep(1.5, 3.0)

            return page.content()

        except PlaywrightTimeout as e:
            logger.error(f"页面加载超时 - URL: {url}, 错误：{str(e)}")
            return None
        except Exception as e:
            logger.error(f"页面加载失败 - URL: {url}, 错误：{str(e)}")
            return None

    def extract_text(self, page, selector: str, url: str) -> str:
        """
        提取元素文本内容（带异常保护）

        Args:
            page: Playwright page 对象
            selector: CSS 选择器
            url: 当前 URL（用于错误报告）

        Returns:
            提取的文本内容，失败返回 None
        """
        try:
            locator = page.locator(selector)

            # 检查元素是否存在
            if not locator.is_visible(timeout=5000):
                logger.warning(f"元素不可见 - URL: {url}, Selector: {selector}")
                return None

            text = locator.inner_text(timeout=5000)

            # 提取后执行防指纹休眠
            anti_fingerprint_sleep(1.0, 2.0)

            return text.strip()

        except Exception as e:
            logger.error(
                f"DOM 提取失败 - URL: {url}, "
                f"CSS Selector: {selector}, "
                f"错误：{str(e)}, "
                f"变量快照：{{'headless': {self.headless}, 'base_url': {self.base_url}}}"
            )
            return None

    def extract_all_text(self, page, selector: str, url: str) -> list:
        """
        提取所有匹配元素的文本内容（带异常保护）

        Args:
            page: Playwright page 对象
            selector: CSS 选择器
            url: 当前 URL（用于错误报告）

        Returns:
            文本列表，失败返回空列表
        """
        try:
            locators = page.locator(selector).all()
            texts = []

            for i, locator in enumerate(locators):
                try:
                    text = locator.inner_text(timeout=3000)
                    if text:
                        texts.append(text.strip())
                except Exception as inner_e:
                    logger.warning(
                        f"单个元素提取失败 - URL: {url}, "
                        f"Selector: {selector}, Index: {i}, "
                        f"错误：{str(inner_e)}"
                    )
                    continue

            # 提取后执行防指纹休眠
            if texts:
                anti_fingerprint_sleep(1.0, 2.0)

            return texts

        except Exception as e:
            logger.error(
                f"批量 DOM 提取失败 - URL: {url}, "
                f"CSS Selector: {selector}, "
                f"错误：{str(e)}, "
                f"变量快照：{{'headless': {self.headless}}}"
            )
            return []

    def crawl_champion_list(self) -> dict:
        """
        爬取英雄列表数据

        Returns:
            英雄数据字典
        """
        url = f"{self.base_url}/champions"
        logger.info(f"开始爬取英雄列表：{url}")

        result = {
            "success": False,
            "url": url,
            "champions": [],
            "error": None
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080}
                )
                page = context.new_page()

                # 获取页面
                html = self.fetch_page(url, page)
                if html is None:
                    result["error"] = "页面加载失败"
                    return result

                # 提取英雄数据（精准选择器）
                selectors_to_try = [".champ-card .name"]

                for selector in selectors_to_try:
                    champions = self.extract_all_text(page, selector, url)
                    if champions:
                        logger.info(f"成功提取 {len(champions)} 个英雄")
                        result["champions"] = champions
                        result["success"] = True
                        break

                if not result["success"]:
                    logger.warning(f"未找到匹配的英雄元素，尝试备用选择器")
                    # 尝试提取页面标题作为兜底
                    page_title = self.extract_text(page, "title", url)
                    if page_title:
                        logger.info(f"页面标题：{page_title}")

                browser.close()

        except Exception as e:
            logger.error(
                f"爬虫执行异常 - URL: {url}, "
                f"错误：{str(e)}, "
                f"变量快照：{{'user_agent': {self.user_agent[:30]}..., 'headless': {self.headless}}}"
            )
            result["error"] = str(e)

        return result

    def extract_hextech_synergies(self, detail_url: str) -> list:
        """
        提取英雄详情页的海克斯协同方案（带'强力联动'或'陷阱'标签）

        Args:
            detail_url: 英雄详情页 URL

        Returns:
            协同方案列表，每个元素为扁平化的文本行
        """
        logger.info(f"开始提取海克斯协同方案：{detail_url}")
        result = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080}
                )
                page = context.new_page()

                # 加载详情页
                html = self.fetch_page(detail_url, page)
                if html is None:
                    logger.error(f"详情页加载失败：{detail_url}")
                    browser.close()
                    return result

                # 定位卡片并提取内容
                cards = page.locator('.interaction-grid > div').all()
                logger.info(f"找到 {len(cards)} 个交互卡片")

                for card in cards:
                    try:
                        text = card.inner_text()
                        # 检查是否包含目标标签
                        if '强力联动' in text or '陷阱' in text:
                            # 扁平化处理：只保留非空行
                            flattened = " | ".join([line.strip() for line in text.split('\n') if line.strip()])
                            if flattened:
                                result.append(flattened)
                                logger.info(f"提取到协同方案：{flattened[:50]}...")
                    except Exception as e:
                        logger.warning(f"单个卡片提取失败：{str(e)}")
                        continue

                browser.close()
                logger.info(f"成功提取 {len(result)} 个海克斯协同方案")
                return result

        except Exception as e:
            logger.error(f"提取异常 - URL: {detail_url}, 错误：{str(e)}")
            return result

    def crawl_match_history(self, match_id: str = None) -> dict:
        """
        爬取比赛历史数据

        Args:
            match_id: 比赛 ID，可选

        Returns:
            比赛数据字典
        """
        if match_id:
            url = f"{self.base_url}/matches/{match_id}"
        else:
            url = f"{self.base_url}/matches"

        logger.info(f"开始爬取比赛数据：{url}")

        result = {
            "success": False,
            "url": url,
            "matches": [],
            "error": None
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080}
                )
                page = context.new_page()

                # 获取页面
                html = self.fetch_page(url, page)
                if html is None:
                    result["error"] = "页面加载失败"
                    return result

                # 提取比赛数据（选择器需根据实际页面结构调整）
                selectors_to_try = [
                    ".match-item",
                    ".match",
                    "[data-match]",
                    ".match-list tr",
                ]

                for selector in selectors_to_try:
                    matches = self.extract_all_text(page, selector, url)
                    if matches:
                        logger.info(f"成功提取 {len(matches)} 场比赛")
                        result["matches"] = matches
                        result["success"] = True
                        break

                browser.close()

        except Exception as e:
            logger.error(
                f"爬虫执行异常 - URL: {url}, "
                f"错误：{str(e)}, "
                f"变量快照：{{'match_id': {match_id}}}"
            )
            result["error"] = str(e)

        return result


def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("ApexLoL Playwright 爬虫启动")
    logger.info("=" * 50)

    # 创建爬虫实例
    spider = ApexSpider(headless=True)

    # 爬取英雄列表
    logger.info("-" * 30)
    logger.info("【任务 1】爬取英雄列表")
    champion_result = spider.crawl_champion_list()

    if champion_result["success"]:
        logger.info(f"英雄列表爬取成功，共 {len(champion_result['champions'])} 条数据")
        for champ in champion_result["champions"][:5]:  # 只显示前 5 个
            logger.info(f"  - {champ}")
    else:
        logger.error(f"英雄列表爬取失败：{champion_result.get('error')}")

    # 爬取比赛历史（该站点无 matches 路由，已禁用）
    # logger.info("-" * 30)
    # logger.info("【任务 2】爬取比赛历史")
    # match_result = spider.crawl_match_history()
    #
    # if match_result["success"]:
    #     logger.info(f"比赛历史爬取成功，共 {len(match_result['matches'])} 条数据")
    #     for match in match_result["matches"][:3]:
    #         logger.info(f"  - {match}")
    # else:
    #     logger.error(f"比赛历史爬取失败：{match_result.get('error')}")

    logger.info("=" * 50)
    logger.info("爬虫执行完成")
    logger.info("=" * 50)

    # 提取海克斯协同方案（不祥之刃）
    logger.info("-" * 30)
    logger.info("【任务 3】提取海克斯协同方案")
    test_url = "https://apexlol.info/zh/champions/Katarina"
    synergies = spider.extract_hextech_synergies(test_url)
    if synergies:
        logger.info(f"成功提取 {len(synergies)} 个协同方案：")
        for i, synergy in enumerate(synergies, 1):
            if synergy.strip():
                logger.info(f"  [{i}] {synergy}")
    else:
        logger.warning("未找到符合条件的协同方案")

    return {
        "champions": champion_result
    }


if __name__ == "__main__":
    main()
