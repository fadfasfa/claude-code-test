# Scrapling 抓取端迁移计划

本计划记录本轮将仓库级抓取端从旧实现全量迁移到 Scrapling 的实现边界。

## 范围

- 删除旧抓取端仓库级 skill、inventory 引用和 `run/crawler/` 旧工具层。
- 新增 `scrapling-web-scraping` 仓库级 Codex skill。
- 在 `run/crawler/` 提供同步、HTML 优先、Codex 调用的 Scrapling 最小客户端。
- 不修改 `.claude/**`、全局配置、hook、凭据、proxy、`run/scraping/**`、`heybox/**`、`run/data/**`、`QuantProject/**` 或现有业务爬虫。

## 接口

`from crawler import fetch_page` 是 Codex 的同步调用入口。默认 `mode="get"` 使用普通 HTTP 抓取；`mode="browser"` 和 `mode="stealthy"` 只由调用方显式选择。工具层返回 HTML 和可选 CSS selector 命中结果，不做 Markdown/text 转换。

## 设计决策

- 使用同步接口，避免 Codex 一次性调用时额外包 `asyncio.run` 或处理 Windows event loop 差异。
- HTML 优先，不引入 Markdown 转换依赖；结构化字段清洗由调用方按目标页面完成。
- `stealthy` 只做文档级合规约束，不在工具层加运行时守卫，避免把业务授权判断写死在通用客户端里。
- `timeout_ms` 统一按毫秒传入 Scrapling 三类 fetcher。

## 验证

- `quick_validate.py .agents/skills/scrapling-web-scraping`
- `cd run && python -m crawler.smoke_scrapling`
- 搜索确认有效工作树中不再保留旧抓取端引用。
- `git diff --check`
