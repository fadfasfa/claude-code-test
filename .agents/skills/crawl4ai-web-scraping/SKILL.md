---
name: crawl4ai-web-scraping
description: 用于本仓通用网页抓取、Crawl4AI 接入、LLM-ready Markdown 清洗、JSON extraction、RAG/Agent 网页内容采集，以及评估是否用 Crawl4AI 替换现有爬虫。触发于用户明确提到 Crawl4AI、网页抓取、网页转 Markdown/JSON、动态网页清洗、爬虫替换评估或面向大模型的网页内容抽取；不用于普通代码维护、前端 UI、仓库清理、PR 审查、发布或完成前验证。
---

# Crawl4AI 网页抓取适配

本 skill 约束在 `claudecode` 仓库中使用 Crawl4AI 做网页抓取、清洗和替换评估时的边界；它只提供接入判断和执行流程，不改变仓库级安全、Git、验证或工作区规则。

## 默认流程

1. 先读目标 work area 的 `PROJECT.md`、README、附近抓取代码和 `docs/workflows/work_area_registry.md`。
2. 明确目标 URL、输出格式、写入目录、是否允许网络访问、是否允许依赖变更，以及是否只是评估替换。
3. 优先提出最小接入方案；如果要替换现有爬虫，必须先给计划并等待用户确认。
4. 输出数据只能落到目标 work area 已有的 source、out、data 或 reports 约定目录，不写仓库根。
5. 修改后按目标 work area 的最小有效验证收尾；没有验证证据时不要宣称完成。

## 冲突边界

- 非琐碎代码、脚本、配置或 workflow 实现仍必须同时使用 `karpathy-project-bridge`。
- 前端 UI、视觉或交互任务仍归 `frontend-design-project-bridge`。
- 仓库维护、清理候选、保护资产检查和健康检查仍归 `repo-maintenance`。
- 新增或扩展长期工具、hook、workflow module、工作区或额外 skill 前仍归 `repo-module-admission`。
- commit 或 PR 前本地审查仍归 `repo-local-pr-review`。
- 声明完成前仍按 `repo-verification-before-completion` 汇总证据、验证结果和剩余风险。

## 安全边界

- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json` 或私有配置。
- 不默认安装 `crawl4ai`、Playwright 浏览器或其他依赖；依赖变更必须在计划里单独列出并得到确认。
- 不默认绕过验证码、登录墙、robots.txt、网站 ToS 或反爬限制；遇到这些场景先报告合规和技术风险。
- 不默认替换 `heybox/`、`sm2-randomizer/`、`run/` 现有爬虫；替换必须限定目标工作区、输出契约和回滚方式。
- 不新增自动抓取 hook、后台任务、定时任务或发布流程。

## 接入口径

- 文档/RAG 抓取：优先输出 Markdown 或结构化 JSON，并记录源 URL、抓取时间和清洗假设。
- 动态网页：优先确认现有 Playwright 或浏览器依赖是否已经存在；不要为了试验扩大项目依赖面。
- 替换评估：先对比现有输出契约、失败诊断、日志、缓存和验证命令，再决定是否值得替换。
- Firecrawl 或其他托管抓取服务不属于本 skill 初版范围；如需双工具适配，先走 `repo-module-admission`。

## 已落地工具层

Crawl4AI 工具层已在 `run/crawler/` 落地，与 `run/scraping/` 现有爬虫完全平行，互不依赖：

- 依赖安装文件：`run/crawler/requirements-crawl4ai.txt`（crawl4ai + playwright）
- 用户需手动执行一次安装：`pip install -r run/crawler/requirements-crawl4ai.txt` 和 `python -m playwright install chromium`
- CC/CX 调用方式：`from crawler.crawl4ai_client import fetch_page`（在 `run/` 目录下执行）
- 验证：`cd run && python -m crawler.smoke_crawl4ai`
- **Python 版本注意**：本机有 3.11 和 3.13 两套，crawl4ai 安装在 3.13；`python` 命令默认指向 3.11，调用时需显式使用 `C:/Users/apple/AppData/Local/Programs/Python/Python313/python.exe -X utf8`，或将 PATH 中的 3.13 提前。

"不默认安装依赖"规则仍然有效：CC/CX 执行任务时不自动 `pip install`，安装动作由用户主动触发。
