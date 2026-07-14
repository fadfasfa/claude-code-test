---
name: scrapling-web-scraping
description: 用于本仓评估、维护或扩展 Scrapling 网页抓取、动态页面获取、HTML 优先结构化抽取和现有爬虫替换方案。旧 run/crawler 入口不存在；现行 runtime 位于 run/hextech/scraping/transport，仅在目标工作区和依赖可用时执行。
---

# Scrapling 网页抓取评估

## 当前状态

旧 `run/crawler/` 入口及其命令不存在，不得恢复或引用。当前有效 runtime 位于 `run/hextech/scraping/transport/`，包含 `scrapling_client.py`、`requirements-scrapling.txt` 和 `smoke_scrapling.py`，并已被 Hextech 与 synergy 抓取代码调用。执行前仍需检查目标工作区规则、Python 环境和依赖状态。

## 触发

- 用户明确提到 Scrapling、网页抓取、动态网页、adaptive selector、spider、现有 Scrapling 实现维护或爬虫替换评估。
- 不用于普通代码维护、前端 UI、仓库清理、PR 审查或发布。

## 评估口径

1. 先读取目标工作区的现有抓取代码、输出契约和附近规则。
2. 明确目标 URL、输出格式、写入位置、网络与依赖边界，以及本轮是评估还是实现。
3. 优先静态 HTTP/HTML 获取；只有页面确实依赖浏览器渲染时才建议 browser 模式。
4. stealth/anti-bot 能力只在用户明确要求、目标合规且风险已说明时列为候选。
5. 维护现有实现时优先复用 `hextech.scraping.transport.scrapling_client`；只有现行 runtime 无法满足已确认需求时，才评估扩展 API 或新增实现。

## 现行入口

- 依赖：`run/hextech/scraping/transport/requirements-scrapling.txt`
- 客户端：`run/hextech/scraping/transport/scrapling_client.py`
- 冒烟验证：从 `run/` 执行 `.venv\Scripts\python.exe -m hextech.scraping.transport.smoke_scrapling`
- 以上命令仅在目标工作区规则允许且 `.venv` 与依赖已存在时运行；不要为审查或评估自动安装依赖。

## 安全边界

- 不读取或修改 credential、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json` 或私有配置。
- 不默认安装 Scrapling、Playwright、Patchright 或浏览器依赖。
- 不默认绕过验证码、登录墙、robots.txt、网站 ToS 或反爬限制。
- 不默认替换现有爬虫，不新增 hook、后台任务、定时任务或发布流程。
- 未获当前任务授权时，不触碰受保护业务工作区。

## 输出

- 当前抓取实现与缺口
- Scrapling 是否适合，以及静态、browser、stealth 三种模式的必要性
- 建议的最小接入面、依赖变化、验证和回滚边界
- 合规风险与仍需用户决定的高影响项
