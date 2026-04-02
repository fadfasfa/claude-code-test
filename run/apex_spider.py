# 海克斯信息爬虫
# 这是一个轻量级并发爬虫，使用请求库获取页面内容，再用网页解析库提取数据，
# 并配合线程池实现多线程快速抓取。

import logging
import json
import os
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# 常见桌面浏览器请求标识池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    # 获取随机请求标识
    return random.choice(USER_AGENTS)


def normalize_name(name_str: str) -> str:
    # 规范化英雄名称，统一为小写并去掉空格和特殊符号
    if not name_str:
        return ""
    return name_str.replace(" ", "").replace("-", "").replace("'", "").replace(".", "").lower()


class ApexSpider:
    # 轻量级爬虫类，使用请求库进行网络请求，并支持线程池并发抓取静态页面。

    def __init__(self):
        # 初始化爬虫，创建 Session 并挂载随机请求头和重试机制
        self.base_url = "https://apexlol.info/zh"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": get_random_user_agent()
        })
        
        # 配置重试策略：重试 3 次，退避因子为 0.5，针对 429 和服务器错误状态码
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        
        # 为普通和加密协议分别挂载适配器
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        logger.info(f"ApexSpider 初始化完成，User-Agent: {self.session.headers['User-Agent'][:50]}...，重试机制已启用")

    def fetch_page(self, url: str) -> str:
        # 获取页面内容，失败返回 None
        max_retries = 3
        backoff_factor = 0.5
        retryable_status_codes = {429, 500, 502, 503, 504}

        for attempt in range(max_retries + 1):
            try:
                logger.info(f"正在加载页面：{url} (尝试 {attempt + 1}/{max_retries + 1})")
                response = self.session.get(url, timeout=10)
                response.encoding = 'utf-8'

                if response.status_code == 200:
                    return response.text
                elif response.status_code in retryable_status_codes and attempt < max_retries:
                    # 计算退避延迟
                    delay = backoff_factor * (2 ** attempt)
                    logger.warning(f"页面返回可重试状态码：{response.status_code}, URL: {url}, "
                                  f"将在 {delay:.2f} 秒后重试...")
                    import time
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"页面返回状态码异常：{response.status_code}, URL: {url}")
                    return None

            except requests.Timeout:
                if attempt < max_retries:
                    delay = backoff_factor * (2 ** attempt)
                    logger.warning(f"页面加载超时 - URL: {url}, 将在 {delay:.2f} 秒后重试...")
                    import time
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"页面加载超时 - URL: {url}")
                    return None
            except requests.RequestException as e:
                if attempt < max_retries:
                    delay = backoff_factor * (2 ** attempt)
                    logger.warning(f"页面加载失败 - URL: {url}, 错误：{str(e)}, 将在 {delay:.2f} 秒后重试...")
                    import time
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"页面加载失败 - URL: {url}, 错误：{str(e)}")
                    return None
            except Exception as e:
                logger.error(f"页面加载异常 - URL: {url}, 错误：{str(e)}")
                return None

    def crawl_champion_list(self) -> dict:
        # 爬取英雄列表，返回名称和详情页地址
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

            # 解析网页内容
            soup = BeautifulSoup(html, 'html.parser')

            # 定位英雄卡片并提取名称和地址
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

                    # 提取链接属性
                    href = card.get('href')
                    if href:
                        # 拼接完整地址
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
        # 提取英雄详情页中的海克斯协同方案
        logger.info(f"开始提取海克斯协同方案：{detail_url}")
        result = []

        try:
            # 加载详情页
            html = self.fetch_page(detail_url)
            if html is None:
                logger.error(f"详情页加载失败：{detail_url}")
                return result

            # 解析网页内容
            soup = BeautifulSoup(html, 'html.parser')

            # 定位卡片并提取内容，更新选择器以匹配新的页面结构
            cards = soup.select('.interaction-card')
            logger.info(f"找到 {len(cards)} 个交互卡片")

            for card in cards:
                try:
                    # 检查卡片是否包含协同方案标签（强力联动或陷阱）
                    has_synergy_tag = False

                    # 查找标签元素，基于样式类判断协同方案类型
                    tag_elements = card.select('span.tag-badge')
                    for tag_elem in tag_elements:
                        classes = tag_elem.get('class', [])
                        # 检查是否有协同方案相关的类名
                        if 'tag-synergy' in classes or 'tag-trap' in classes or 'tag-fun' in classes:
                            has_synergy_tag = True
                            break

                    if has_synergy_tag:
                        # 使用文本提取函数，并以“ | ”分隔多行
                        text = card.get_text(separator=' | ', strip=True)
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
    # 主函数入口
    logger.info("=" * 50)
    logger.info("ApexLoL 超频并发爬虫启动")
    logger.info("=" * 50)

    # 创建爬虫实例
    spider = ApexSpider()

    # 爬取英雄列表
    logger.info("-" * 30)
    logger.info("（任务 1）爬取英雄列表")
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
    logger.info("（任务 2）加载本地英雄配置")

    core_data_path = os.path.join(CONFIG_DIR, "Champion_Core_Data.json")
    aliases_path = os.path.join(CONFIG_DIR, "hero_aliases.json")

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

    # 构建输出用的完整数据字典，包含核心字段和别名列表
    core_info_dict = {}
    for champ_id, champ_info in core_data.items():
        name = champ_info.get("name")
        if name:
            core_info_dict[champ_id] = {
                "id": champ_id,
                "name": name,
                "title": champ_info.get("title", ""),
                "en_name": champ_info.get("en_name", ""),
                "aliases": hero_aliases.get(name, [])
            }

    # 构建网页名称匹配索引，只使用名称、称号和英文名
    search_index = {}
    for champ_id, champ_info in core_info_dict.items():
        # 将名称、称号和英文名加入搜索索引
        names_to_index = [
            champ_info["name"],
            champ_info["title"],
            champ_info["en_name"]
        ]

        for name_field in names_to_index:
            if name_field:
                normalized = normalize_name(name_field)
                if normalized:
                    search_index[normalized] = champ_id

    logger.info(f"构建核心数据字典：{len(core_info_dict)} 个英雄")
    logger.info(f"构建搜索索引：{len(search_index)} 个关键词")

    # 全量遍历英雄列表并提取海克斯协同方案，使用线程池并发执行
    logger.info("-" * 30)
    logger.info("（任务 3）全量提取海克斯协同方案（16 线程并发）")

    # 初始化最终数据字典
    final_data = {}

    # 获取英雄列表（全量，移除之前的[:3]限制）
    champions = champion_result.get("champions", [])
    if champions:
        logger.info(f"开始遍历 {len(champions)} 个英雄的海克斯协同方案（并发处理）...")

        # 构建任务字典：地址对应英雄信息
        task_map = {}
        skipped_names = []

        for champ in champions:
            champ_name = champ["name"]
            champ_url = champ["url"]

            # 对网页提取的英雄名做同样清洗，再去搜索索引中查找编号
            normalized_champ_name = normalize_name(champ_name)
            champ_id = search_index.get(normalized_champ_name)

            if not champ_id:
                skipped_names.append(champ_name)
                continue

            # 从核心信息字典中取出完整信息并组装任务
            core_info = core_info_dict[champ_id]
            task_map[champ_url] = {
                "name": core_info["name"],
                "id": champ_id,
                "title": core_info["title"],
                "en_name": core_info["en_name"],
                "aliases": core_info["aliases"]
            }

        # 调试信息
        if skipped_names:
            logger.warning(f"未匹配的英雄名称数: {len(skipped_names)}")
            if len(skipped_names) <= 10:
                for name in skipped_names[:5]:
                    logger.warning(f"  示例: {repr(name)}")

        logger.info(f"成功匹配 {len(task_map)} 个英雄用于并发抓取")

        # 使用线程池进行并发抓取（将工作线程数从 16 调整为 8）
        with ThreadPoolExecutor(max_workers=8) as executor:
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

        # 持久化到数据文件
        os.makedirs(CONFIG_DIR, exist_ok=True)
        output_path = os.path.join(CONFIG_DIR, "Champion_Synergy.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, ensure_ascii=False, indent=2)

        logger.info(f"数据已保存到：{output_path}")
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
