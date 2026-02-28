# -*- coding: utf-8 -*-
"""
ApexLoL 信息爬虫 - 轻量级并发爬虫 (requests + BeautifulSoup)

针对 apexlol.info 的轻量级爬虫，采用 requests 静态获取 + BeautifulSoup 解析，
配合 ThreadPoolExecutor 实现 16 线程并发极速抓取。
"""

import logging
import json
import os
import random
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

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


class ApexSpider:
    """
    ApexLoL 轻量级爬虫类

    使用 requests 进行 HTTP 请求，BeautifulSoup 进行 HTML 解析。
    支持 ThreadPoolExecutor 并发处理，极速抓取静态页面。
    """

    def __init__(self):
        """
        初始化爬虫

        创建 requests.Session() 并挂载随机 User-Agent
        """
        self.base_url = "https://apexlol.info/zh"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": get_random_user_agent()
        })
        logger.info(f"ApexSpider 初始化完成，User-Agent: {self.session.headers['User-Agent'][:50]}...")

    def fetch_page(self, url: str) -> str:
        """
        获取页面内容

        Args:
            url: 目标 URL

        Returns:
            页面 HTML 内容，失败返回 None
        """
        try:
            logger.info(f"正在加载页面：{url}")
            response = self.session.get(url, timeout=10)
            response.encoding = 'utf-8'

            if response.status_code != 200:
                logger.error(f"页面返回状态码异常：{response.status_code}, URL: {url}")
                return None

            return response.text

        except requests.Timeout:
            logger.error(f"页面加载超时 - URL: {url}")
            return None
        except requests.RequestException as e:
            logger.error(f"页面加载失败 - URL: {url}, 错误：{str(e)}")
            return None
        except Exception as e:
            logger.error(f"页面加载异常 - URL: {url}, 错误：{str(e)}")
            return None

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
            # 获取页面
            html = self.fetch_page(url)
            if html is None:
                result["error"] = "页面加载失败"
                return result

            # 解析 HTML
            soup = BeautifulSoup(html, 'html.parser')

            # 定位英雄卡片并提取名称和 URL
            champ_cards = soup.select('.champ-card')
            logger.info(f"找到 {len(champ_cards)} 个英雄卡片")

            champions = []
            for card in champ_cards:
                try:
                    # 提取英雄名称
                    name_elem = card.select_one('.name')
                    if not name_elem:
                        continue

                    name = name_elem.get_text(strip=True)

                    # 提取 href 属性
                    href = card.get('href')
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

        except Exception as e:
            logger.error(
                f"爬虫执行异常 - URL: {url}, "
                f"错误：{str(e)}"
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
            # 加载详情页
            html = self.fetch_page(detail_url)
            if html is None:
                logger.error(f"详情页加载失败：{detail_url}")
                return result

            # 解析 HTML
            soup = BeautifulSoup(html, 'html.parser')

            # 定位卡片并提取内容
            cards = soup.select('.interaction-grid > div')
            logger.info(f"找到 {len(cards)} 个交互卡片")

            for card in cards:
                try:
                    # 使用 get_text 提取文本，以 ' | ' 分隔多行
                    text = card.get_text(separator=' | ', strip=True)
                    # 检查是否包含目标标签
                    if '强力联动' in text or '陷阱' in text:
                        if text:
                            result.append(text)
                            logger.info(f"提取到协同方案：{text[:50]}...")
                except Exception as e:
                    logger.warning(f"单个卡片提取失败：{str(e)}")
                    continue

            logger.info(f"成功提取 {len(result)} 个海克斯协同方案")
            return result

        except Exception as e:
            logger.error(f"提取异常 - URL: {detail_url}, 错误：{str(e)}")
            return result


def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("ApexLoL 超频并发爬虫启动")
    logger.info("=" * 50)

    # 创建爬虫实例
    spider = ApexSpider()

    # 爬取英雄列表
    logger.info("-" * 30)
    logger.info("【任务 1】爬取英雄列表")
    champion_result = spider.crawl_champion_list()

    if champion_result["success"]:
        logger.info(f"英雄列表爬取成功，共 {len(champion_result['champions'])} 条数据")
        for champ in champion_result["champions"][:3]:
            logger.info(f"  - {champ}")
    else:
        logger.error(f"英雄列表爬取失败：{champion_result.get('error')}")
        return

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

    # 全量遍历英雄列表并提取海克斯协同方案（使用 ThreadPoolExecutor 并发）
    logger.info("-" * 30)
    logger.info("【任务 3】全量提取海克斯协同方案（16 线程并发）")

    # 初始化最终数据字典
    final_data = {}

    # 获取英雄列表（全量，移除之前的 [:3] 限制）
    champions = champion_result.get("champions", [])
    if champions:
        logger.info(f"开始遍历 {len(champions)} 个英雄的海克斯协同方案（并发处理）...")

        # 构建任务字典：URL -> 英雄信息
        task_map = {}
        skipped_names = []
        for champ in champions:
            champ_name = champ["name"]
            champ_url = champ["url"]

            # 通过名称匹配核心数据
            if champ_name not in name_to_core:
                skipped_names.append(champ_name)
                continue

            core_info = name_to_core[champ_name]
            champ_id = core_info["id"]

            # 获取别名
            aliases = hero_aliases.get(champ_name, [])

            task_map[champ_url] = {
                "name": champ_name,
                "id": champ_id,
                "title": core_info["title"],
                "en_name": core_info["en_name"],
                "aliases": aliases
            }

        # 调试信息
        if skipped_names:
            logger.warning(f"未匹配的英雄名称数: {len(skipped_names)}")
            if len(skipped_names) <= 10:
                for name in skipped_names[:5]:
                    logger.warning(f"  示例: {repr(name)}")

        logger.info(f"成功匹配 {len(task_map)} 个英雄用于并发抓取")

        # 使用 ThreadPoolExecutor 进行并发抓取
        with ThreadPoolExecutor(max_workers=16) as executor:
            # 提交所有任务
            future_to_url = {
                executor.submit(spider.extract_hextech_synergies, url): url
                for url in task_map.keys()
            }

            # 收集完成的任务结果
            for future in as_completed(future_to_url):
                champ_url = future_to_url[future]
                try:
                    synergies = future.result()
                    champ_info = task_map[champ_url]
                    champ_id = champ_info["id"]
                    champ_name = champ_info["name"]

                    # 合并数据结构
                    final_data[champ_id] = {
                        "id": champ_id,
                        "name": champ_name,
                        "title": champ_info["title"],
                        "en_name": champ_info["en_name"],
                        "aliases": champ_info["aliases"],
                        "synergies": synergies
                    }

                    logger.info(f"[{champ_name}] 提取完成，共 {len(synergies)} 个协同方案")

                except Exception as e:
                    logger.error(f"并发任务异常 - URL: {champ_url}, 错误：{str(e)}")
                    continue

        # 持久化到 JSON 文件
        os.makedirs("run/config", exist_ok=True)
        output_path = "run/config/Champion_Synergy.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, ensure_ascii=False, indent=2)

        logger.info(f"Data saved to: {output_path}")
        logger.info(f"Total heroes captured: {len(final_data)}")
    else:
        logger.error("英雄列表为空，无法提取协同方案")

    logger.info("=" * 50)
    logger.info("爬虫执行完成")
    logger.info("=" * 50)

    return {
        "champions": champion_result,
        "hextech_data": final_data
    }


if __name__ == "__main__":
    main()
