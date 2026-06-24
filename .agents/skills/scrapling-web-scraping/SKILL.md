---
name: scrapling-web-scraping
description: 用于本仓通过 Scrapling 进行网页抓取、动态网页获取、HTML 优先结构化抽取、adaptive selector、spider 编写，以及替换旧抓取端后的默认抓取能力评估。触发于用户明确提到 Scrapling、网页抓取、网页转 HTML/结构化结果、动态网页、stealth/anti-bot 合规抓取、adaptive selector、spider、或评估现有爬虫替换；不用于普通代码维护、前端 UI、仓库清理、PR 审查、发布或完成前验证。
---

# Scrapling 网页抓取适配

## Overview

本 skill 约束在 `claudecode` 仓库中使用 Scrapling 做网页抓取、动态网页获取和结构化抽取时的边界；它只提供接入判断和执行流程，不改变仓库级安全、Git、验证或工作区规则。

`run/crawler/` 工具层的实际调用方是 Codex / OpenAI Codex plugin。Claude Code 不在本仓通过该 skill 调用抓取工具，也不负责抓取业务实现。

默认使用简体中文输出计划、风险、验证和总结；URL、selector、API、路径、命令和错误原文保持原文。

## 默认流程

1. 先读目标 work area 的 `PROJECT.md`、README、附近抓取代码和 `docs/当前规则/10-工作区登记.md`。
2. 明确目标 URL、输出格式、写入目录、是否允许网络访问、是否允许依赖变更，以及是否只是评估替换。
3. 默认从同步 `mode="get"` 开始；只有页面确实依赖浏览器渲染时才升级到 `mode="browser"`。
4. `mode="stealthy"` 只在用户明确授权且目标合规时使用；不要默认启用 stealth/anti-bot 能力。
5. 输出数据只能落到目标 work area 已有的 source、out、data 或 reports 约定目录，不写仓库根。
6. 修改后按目标 work area 的最小有效验证收尾；没有验证证据时不要宣称完成。

## 工具层

Scrapling 工具层落在 `run/crawler/`，与 `run/scraping/` 现有业务爬虫完全平行，互不依赖：

- 依赖安装文件：`run/crawler/requirements-scrapling.txt`
- 手动安装：`pip install -r run/crawler/requirements-scrapling.txt`
- 浏览器能力按需安装：`scrapling install`
- Codex 调用方式：`from crawler import fetch_page`（在 `run/` 目录下执行）
- 验证：`cd run && python -m crawler.smoke_scrapling`

示例：

```python
from crawler import fetch_page

result = fetch_page("https://example.com", mode="get", css_selector="h1::text")
```

## 接入口径

- HTML 优先：工具层返回 HTML 和可选 CSS selector 命中结果，不做 Markdown/text 转换。
- 简单页面：使用 `mode="get"`。
- 动态网页：使用 `mode="browser"`，必要时传入 `wait_for` 或 `network_idle=True`。
- 强保护页面：只有在用户明确授权并确认目标合规后，才考虑 `mode="stealthy"`。
- 结构化抽取：工具层只负责 `extracted` 列表，业务 schema、字段清洗和持久化由调用方完成。

## 冲突边界

- 非琐碎代码、脚本、配置或 workflow 实现仍必须同时使用 `karpathy-project-bridge`。
- 前端 UI、视觉或交互任务仍归 `frontend-design-project-bridge`。
- 仓库维护、清理候选、保护资产检查和健康检查仍归 `repo-maintenance`。
- 新增或扩展长期工具、hook、workflow module、工作区或额外 skill 前仍归 `repo-module-admission`。
- commit 或 PR 前本地审查仍归 `repo-local-pr-review`。
- 声明完成前仍按 `repo-verification-before-completion` 汇总证据、验证结果和剩余风险。

## 安全边界

- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json` 或私有配置。
- 不默认安装 `scrapling`、Playwright/Patchright 浏览器或其他依赖；依赖变更必须在计划里单独列出并得到确认。
- 不默认绕过验证码、登录墙、robots.txt、网站 ToS 或反爬限制；遇到这些场景先报告合规和技术风险。
- 不默认替换 `heybox/`、`sm2-randomizer/`、`run/` 现有爬虫；替换必须限定目标工作区、输出契约和回滚方式。
- 未获当前任务授权时，不触碰 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 或 `qm-run-demo`。
- 不新增自动抓取 hook、后台任务、定时任务或发布流程。
