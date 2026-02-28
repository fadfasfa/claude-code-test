#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小黑盒 Web 接口探针脚本
用于探测游戏数据接口，集成 Scraper-Ninja 逻辑
"""

import asyncio
import json
import random
import logging
from pathlib import Path

import httpx

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data/heybox_probe.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 目标 URL
TARGET_URL = "https://api.xiaoheihe.cn/game/web/all_recommend/games/?os_type=web&version=999.0.0"

# User-Agent 池（Scraper-Ninja 逻辑）
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


async def probe_heybox():
    """
    探测小黑盒 Web 接口
    包含随机 UA、随机延迟，处理 403 和签名错误
    """
    # 随机 User-Agent
    random_ua = random.choice(USER_AGENTS)

    # 随机延迟 1-3 秒（Scraper-Ninja 逻辑）
    delay = random.uniform(1, 3)
    logger.info(f"随机延迟：{delay:.2f} 秒")
    await asyncio.sleep(delay)

    headers = {
        "User-Agent": random_ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.xiaoheihe.cn/",
        "Origin": "https://www.xiaoheihe.cn",
    }

    logger.info(f"使用 User-Agent: {random_ua[:50]}...")
    logger.info(f"目标 URL: {TARGET_URL}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(TARGET_URL, headers=headers)

            # 记录响应头信息
            logger.info(f"响应状态码：{response.status_code}")
            logger.info(f"响应头：{dict(response.headers)}")

            # 处理 403 和签名错误
            if response.status_code == 403:
                logger.error("⚠️  收到 403 Forbidden 响应")
                logger.error(f"完整响应头：{dict(response.headers)}")
                return None

            # 检查签名错误（通常返回特定错误码）
            if response.status_code == 200:
                data = response.json()
                # 检查是否有签名相关的错误码
                if data.get('code') not in [0, None, '0']:
                    logger.error(f"⚠️  接口返回错误码：{data.get('code')}")
                    logger.error(f"错误信息：{data.get('message', 'Unknown')}")
                    logger.error(f"完整响应头：{dict(response.headers)}")
                    return None

            response.raise_for_status()

            # 获取 JSON 数据
            json_data = response.json()

            logger.info("✓ 成功获取数据")

            return json_data

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP 错误：{e}")
            logger.error(f"完整响应头：{dict(e.response.headers)}")
            return None
        except httpx.RequestError as e:
            logger.error(f"请求错误：{e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误：{e}")
            logger.error(f"响应内容：{response.text[:500]}")
            return None


def save_data(data: dict, output_path: str = "data/heybox_sample.json"):
    """
    保存 JSON 数据到文件
    """
    # 确保目录存在
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"✓ 数据已保存至：{output_path}")


async def main():
    """
    主函数
    """
    logger.info("=" * 50)
    logger.info("小黑盒 Web 接口探针启动")
    logger.info("=" * 50)

    data = await probe_heybox()

    if data:
        save_data(data)
        # 打印部分数据预览
        logger.info("数据预览:")
        if isinstance(data, dict):
            preview = json.dumps(data, ensure_ascii=False, indent=2)[:500]
            logger.info(f"{preview}...")
    else:
        logger.error("✗ 数据获取失败，请检查日志中的响应头信息")

    logger.info("=" * 50)
    logger.info("探针执行完毕")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
