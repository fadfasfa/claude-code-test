# -*- coding: utf-8 -*-
"""
ApexLoL 信息爬虫 - Playwright 渲染层独立爬虫

针对 apexlol.info 的独立 Playwright 渲染层爬虫，实施降维打击获取底层数据。
绝对隔离于现有业务域。
"""

import logging
import json
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
        爬取英雄列表数据（包含名称和详情页 URL）

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

                # 定位英雄卡片并提取名称和 URL
                champ_cards = page.locator('.champ-card').all()
                logger.info(f"找到 {len(champ_cards)} 个英雄卡片")

                champions = []
                for card in champ_cards:
                    try:
                        # 提取英雄名称
                        name_locator = card.locator('.name')
                        if name_locator.is_visible(timeout=3000):
                            name = name_locator.inner_text(timeout=3000).strip()
                            # 提取 href 属性
                            href = card.get_attribute('href', timeout=3000)
                            if href:
                                # 拼接完整 URL
                                if href.startswith('/'):
                                    full_url = f"https://apexlol.info{href}"
                                elif href.startswith('http'):
                                    full_url = href
                                else:
                                    full_url = f"https://apexlol.info/{href}"
                                champions.append({"name": name, "url": full_url})
                                logger.info(f"提取英雄：{name} -> {full_url}")
                    except Exception as e:
                        logger.warning(f"单个英雄卡片提取失败：{str(e)}")
                        continue

                if champions:
                    logger.info(f"成功提取 {len(champions)} 个英雄（含 URL）")
                    result["champions"] = champions
                    result["success"] = True
                else:
                    logger.warning("未找到匹配的英雄元素")

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

    # 加载本地配置文件
    logger.info("-" * 30)
    logger.info("【任务 2】加载本地英雄配置")

    core_data_path = "run/config/Champion_Core_Data.json"
    aliases_path = "run/config/hero_aliases.json"

    try:
        with open(core_data_path, "r", encoding="utf-8") as f:
            core_data = json.load(f)
        logger.info(f"核心数据加载成功：{len(core_data)} 个英雄")
    except Exception as e:
        logger.error(f"核心数据加载失败：{e}")
        core_data = {}

    try:
        with open(aliases_path, "r", encoding="utf-8") as f:
            hero_aliases = json.load(f)
        logger.info(f"别名数据加载成功：{len(hero_aliases)} 个英雄")
    except Exception as e:
        logger.error(f"别名数据加载失败：{e}")
        hero_aliases = {}

    # 构建 name_to_core 查找字典
    name_to_core = {}
    for champ_id, champ_info in core_data.items():
        name = champ_info.get("name")
        if name:
            name_to_core[name] = {
                "id": champ_id,
                "title": champ_info.get("title", ""),
                "en_name": champ_info.get("en_name", "")
            }
    logger.info(f"构建名称索引：{len(name_to_core)} 个英雄")

    # 全量遍历英雄列表并提取海克斯协同方案
    logger.info("-" * 30)
    logger.info("【任务 3】全量提取海克斯协同方案")

    # 初始化最终数据字典
    final_data = {}

    # 获取英雄列表
    champions = champion_result.get("champions", [])
    if champions:
        # 测试模式：只抓取前 3 个英雄（移除 [:3] 以抓取全量 172 个英雄）
        test_champions = champions[:3]  # 移除 [:3] 以抓取全量 172 个英雄
        logger.info(f"开始遍历 {len(test_champions)} 个英雄的海克斯协同方案...")

        for champ in test_champions:
            champ_name = champ["name"]
            champ_url = champ["url"]

            # 通过名称匹配核心数据
            if champ_name not in name_to_core:
                logger.warning(f"未找到英雄 [{champ_name}] 的核心数据，跳过")
                continue

            core_info = name_to_core[champ_name]
            champ_id = core_info["id"]

            # 获取别名（从 hero_aliases 反向查找）
            aliases = []
            for alias_name, alias_list in hero_aliases.items():
                if alias_name == champ_name:
                    aliases = alias_list
                    break

            logger.info(f"正在提取 [{champ_name}] 的协同方案：{champ_url}")
            synergies = spider.extract_hextech_synergies(champ_url)

            # 合并数据结构
            final_data[champ_id] = {
                "id": champ_id,
                "name": champ_name,
                "title": core_info["title"],
                "en_name": core_info["en_name"],
                "aliases": aliases,
                "synergies": synergies
            }

            logger.info(f"[{champ_name}] 提取完成，共 {len(synergies)} 个协同方案")

        # 持久化到 JSON 文件
        os.makedirs("run/data", exist_ok=True)
        output_path = "run/data/apex_hextech_data.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, ensure_ascii=False, indent=2)

        logger.info(f"数据已保存至：{output_path}")
        logger.info(f"总计抓取 {len(final_data)} 个英雄的海克斯数据")
    else:
        logger.error("英雄列表为空，无法提取协同方案")

    return {
        "champions": champion_result,
        "hextech_data": final_data
    }


if __name__ == "__main__":
    main()
