---
name: scrapling-web-scraping
description: 用于本仓评估、维护或扩展 Scrapling 网页抓取、动态页面获取、HTML 优先结构化抽取，以及判断 Scrapling 与 Firecrawl 的仓内取舍。旧 run/crawler 入口不存在；现行 runtime 位于 run/src/hextech/infrastructure/transport，仅在目标工作区和依赖可用时执行。
---

# Scrapling 网页抓取评估

## 当前状态

旧 `run/crawler/` 入口及其命令不存在，不得恢复或引用。当前有效 runtime 位于 `run/src/hextech/infrastructure/transport/`，包含 `scrapling_client.py` 和 `smoke_scrapling.py`；依赖由 `run/pyproject.toml` 统一声明，并已被 Hextech 与 synergy 抓取代码调用。执行前仍需检查目标工作区规则、Python 环境和依赖状态。

Scrapling 上游提供 adaptive selector、StealthyFetcher、Spider、持久 session 和 proxy rotation 等能力；本仓当前生产 wrapper 只公开 `FetchMode = "get" | "browser"`，即静态 `Fetcher` 与普通 `DynamicFetcher`。不得把上游未暴露能力描述成当前系统已经具备，也不得通过恢复旧入口绕开现行 transport。

实际版本必须现场核对 `run/pyproject.toml` 的约束，并在 `.venv` 存在时查询 `importlib.metadata.version("scrapling")`；Skill 不固定记录某次安装版本。

## 触发

- 用户明确提到 Scrapling、网页抓取、动态网页、adaptive selector、spider、现有 Scrapling 实现维护或爬虫替换评估。
- 用户要求比较 Scrapling 与 Firecrawl，或判断 Firecrawl 是否应进入本仓抓取链路。
- 不用于普通代码维护、前端 UI、仓库清理、PR 审查或发布。

## 评估口径

1. 先读取目标工作区的现有抓取代码、输出契约和附近规则。
2. 明确目标 URL、输出格式、写入位置、网络与依赖边界，以及本轮是评估还是实现。
3. 优先静态 HTTP/HTML 获取；只有页面确实依赖浏览器渲染时才建议 browser 模式。
4. 本仓 wrapper 未暴露 stealth；stealth/anti-bot 能力只在用户明确要求、目标合规且风险已说明时列为扩展候选。
5. 维护现有实现时优先复用 `hextech.infrastructure.transport.scrapling_client`；只有现行 runtime 无法满足已确认需求时，才评估扩展 API 或新增实现。

## 与全局研究和 Firecrawl 的边界

- 普通开放网络发现、多来源研究和页面语义读取走用户级网络研究路由与 Exa，不调用本仓 Scrapling 生产 transport。
- 已知业务来源、确定性 HTML/JSON、现有解析合同和 current/last-good 链路继续使用 Scrapling。
- Firecrawl 只在整站 Crawl/Map、托管批量抓取、Webhook、LLM schema extraction、批量 PDF/OCR、跨地区托管代理或显著降低运维成本成为已确认需求时进入候选。
- Firecrawl 不得成为 Scrapling 的自动 fallback。替换现行 transport 前必须另有迁移设计，说明第三方数据流、认证、成本／配额、保留策略、失败合同、来源 provenance、验证与回滚。
- Firecrawl 试验必须由用户另行授权并隔离执行；不得写生产资源、接管 current/last-good、读取凭据或把 probe 成功当作迁移完成。
- Firecrawl Cloud 与自建版能力不同；评估时必须核实目标部署实际具备的渲染、代理、动作、截图和反爬能力，不以 Cloud 宣传能力代表默认自建栈。

## 现行入口

- 依赖：`run/pyproject.toml`
- 客户端：`run/src/hextech/infrastructure/transport/scrapling_client.py`
- 冒烟验证：从 `run/` 执行 `.venv\Scripts\python.exe -m hextech.infrastructure.transport.smoke_scrapling`
- 以上命令仅在目标工作区规则允许且 `.venv` 与依赖已存在时运行；不要为审查或评估自动安装依赖。

## 安全边界

- 不读取或修改 credential、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json` 或私有配置。
- 不默认安装 Scrapling、Playwright、Patchright 或浏览器依赖。
- 不默认安装或连接 Firecrawl，不注册账号，不创建、读取或保存 Firecrawl API key。
- 不默认绕过验证码、登录墙、robots.txt、网站 ToS 或反爬限制。
- 不默认替换现有爬虫，不新增 hook、后台任务、定时任务或发布流程。
- 未获当前任务授权时，不触碰受保护业务工作区。

## 输出

- 当前抓取实现与缺口
- Scrapling 上游能力与本仓已暴露能力的差异
- Scrapling 是否适合，以及静态、browser、stealth 三种模式的必要性
- Firecrawl 是否补足了已确认缺口，以及 Cloud／自建、数据流、配额和迁移成本
- 建议的最小接入面、依赖变化、验证和回滚边界
- 合规风险与仍需用户决定的高影响项
