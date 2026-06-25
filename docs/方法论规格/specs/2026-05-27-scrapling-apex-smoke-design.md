# Scrapling × CloakBrowser ApexLoL 联动抓取双路 smoke 设计

本设计描述如何在新工作树里用 scrapling get 与 CloakBrowser 两条独立路径，验证能否抓到 ApexLoL 海克斯联动详情页的 hydration 数据；执行交给 Codex，本仓 CC 不动手。

## 1. 背景

- 现有联动抓取栈 [run/scraping/full_synergy_scraper.py](../../../run/scraping/full_synergy_scraper.py) 在 requests 失败时回退 Selenium + Edge/Chrome 自管 profile；维护成本与 Cloudflare 适应性都偏重。
- 本仓在 [run/crawler/](../../../run/crawler/) 已落 scrapling 客户端 ([scrapling_client.py](../../../run/crawler/scrapling_client.py)) 与 example.com smoke，但**未在真实 ApexLoL 详情页验证过**；scrapling 0.4.8 的 StealthyFetcher 只支持 Camoufox，且未来版本是否原生集成新反指纹引擎不可预期。
- CloakBrowser（patched Chromium，~200MB binary）反指纹层级覆盖 Cloudflare Turnstile 与 reCAPTCHA v3，但**不能塞进 scrapling StealthyFetcher 槽位**；二者只能通过隔离边界共存。

## 2. 目标

为后续是否扩大 scrapling 使用面 / 是否把反爬层切到 CloakBrowser 提供可重复的证据，避免在 scrapling 业务化落盘之前再返工。

具体可观测目标：
1. **路径 1（scrapling get）**：用 `Fetcher.get` 抓 `https://apexlol.info/zh/champions/Vi`，判定能否拿到联动 hydration marker。
2. **路径 2（CloakBrowser）**：用 `cloakbrowser.launch()` 直接 `page.goto` 同一 URL，判定能否拿到同样的 marker，并对照路径 1 是否被 Cloudflare 拦。
3. **边界证据**：两条路径完全解耦，未来切换任一底层不需要改另一侧。

## 3. 非目标

- 不修改 [full_synergy_scraper.py](../../../run/scraping/full_synergy_scraper.py)、[full_hextech_scraper.py](../../../run/scraping/full_hextech_scraper.py) 或任何业务运行时代码。
- 不删除/替换已有 [smoke_scrapling.py](../../../run/crawler/smoke_scrapling.py)（通用 example.com 验收保留）。
- 不安装 Camoufox。
- 不接入 Camoufox 与 CloakBrowser 对照组（本次只验证 CloakBrowser 替代槽位是否成立）。
- 不做并发、不做多英雄遍历、不做数据写库。

## 4. 隔离边界与单元

| 单元 | 文件 | 单一职责 | 依赖 |
|---|---|---|---|
| scrapling HTTP 抓取层 | 已有 `run/crawler/scrapling_client.py` | 同源 HTTP 抓取，返回 HTML | `scrapling[fetchers]` |
| scrapling smoke 入口 | 新增 `run/crawler/smoke_apex_scrapling_get.py` | 用 scrapling get 抓一个 ApexLoL 详情页并写产物 | `crawler.scrapling_client` + `crawler.apex_markers` |
| CloakBrowser 抓取层 | 新增 `run/crawler/cloakbrowser_client.py` | 启 CloakBrowser → `page.goto` → 返回 HTML | `cloakbrowser` |
| CloakBrowser smoke 入口 | 新增 `run/crawler/smoke_apex_cloakbrowser.py` | 用 CloakBrowser 抓同一详情页并写产物 | `crawler.cloakbrowser_client` + `crawler.apex_markers` |
| 共享判定 util | 新增 `run/crawler/apex_markers.py` | 给 HTML 打 `has_synergy_marker` / `has_cf_block` 标签；估算联动条数 | 标准库（`re`、`json`、`html`），不引入第三方解析器 |
| CloakBrowser 依赖钉版 | 新增 `run/crawler/requirements-cloakbrowser.txt` | `cloakbrowser>=0.3,<0.4` 单行 | — |

约束：
- `scrapling_client.py` 与 `cloakbrowser_client.py` 互不 import。
- 两个 smoke 入口除 `apex_markers` 外不互相 import。
- 共享 util 只做"读 HTML，吐 bool 与计数"，不引入抓取逻辑。

## 5. 接口与命令行约定

两个 smoke 入口共用形参：

```
python -m crawler.smoke_apex_scrapling_get [--url URL] [--out DIR]
python -m crawler.smoke_apex_cloakbrowser   [--url URL] [--out DIR]
```

- `--url`：默认 `https://apexlol.info/zh/champions/Vi`。
- `--out`：默认 `data/runtime/reports/scrapling_apex_smoke/<YYYYMMDD_HHMMSS>_<mode>/`，相对 `run/` 工作目录解析。

CloakBrowser smoke 必传一组运行参数（不暴露 CLI）：
- `headless=True`
- `humanize=False`（本次只做静态抓取，不引入行为模拟以避免 binary 行为不确定性）
- `page.goto(url, wait_until="networkidle", timeout=30000)`
- `page.content()` 取页面 HTML

scrapling smoke 沿用 [scrapling_client.fetch_page](../../../run/crawler/scrapling_client.py) `mode="get"`、`timeout_ms=30000`、不传 css_selector。

两个 smoke 都要求在 worktree 内 `run/` 目录下用 `python -m crawler.<entry>` 形式启动，与现有 [smoke_scrapling.py](../../../run/crawler/smoke_scrapling.py) 启动约定一致；`--out` 默认值是相对该 cwd 解析。

## 6. 产物布局

每条 smoke 在自己的 `--out` 目录里落三件物：

- `result.json`
  ```json
  {
    "url": "https://apexlol.info/zh/champions/Vi",
    "mode": "scrapling_get" | "cloakbrowser",
    "status_code": 200 | null,
    "fetched_at": "2026-05-27T...+00:00",
    "html_length": 123456,
    "has_synergy_marker": true | false,
    "has_cf_block": true | false,
    "synergy_entry_count": 0,
    "error": null | "<exception summary>"
  }
  ```
- `page.html.txt`：HTML 截断到 200 KB，UTF-8。
- `stderr.log`：抓取过程的标准错误。

## 7. 验收判定

`apex_markers.py` 输出三项：

- `has_synergy_marker`：HTML 包含 "评分" 且包含 "强力联动|陷阱|娱乐|缺陷" 中任一项；或 `<script id="__NEXT_DATA__">` 文本含 `synergy` 或 `augment`。
- `has_cf_block`：HTML 前 8000 字 lower 后同时含 `attention required` 与 `cloudflare`（与 [full_synergy_scraper.py:706-708](../../../run/scraping/full_synergy_scraper.py#L706-L708) 一致）。
- `synergy_entry_count`：尝试解析任一 hydration script（`__NEXT_DATA__`、`__NUXT_DATA__`、`__remixContext` 或 React Flight `__next_f.push` 段），统计 `synergies` 或 `synergy_items` 数组长度，解析不出来填 0；不做精确校准，仅作粗略量纲参考。

退出码：
- `0` = `has_synergy_marker == True` 且 `has_cf_block == False`。
- `1` = 拿到 HTML 但 marker 缺失，或被 CF 拦截。
- `2` = 环境缺失（scrapling 或 cloakbrowser 未安装、CloakBrowser binary 首次下载失败、网络 unreachable）。

两条 smoke 跑完后整体结论矩阵：

| scrapling get | CloakBrowser | 结论 |
|---|---|---|
| 0 | 任意 | **PASS（最佳）**——同源 HTTP 已够 |
| 1 (CF 拦) | 0 | **PASS**——CloakBrowser 反爬兜底有效 |
| 1 (无 marker) | 0 | **PASS 但需注意**——可能动态 hydration 问题，需要更细排查 |
| 任意 | 1 / 2 | **FAIL**——记录产物后停下，等用户决定 |

## 8. 执行环境

- worktree 路径：`C:\Users\apple\_worktrees\cc\claudecode-cc-probe-scrapling-apex-smoke`
- 分支：`cc/probe/scrapling-apex-smoke`，base `main`
- 命名遵循 [docs/当前规则/20-Git与高危操作.md](../../当前规则/20-Git与高危操作.md) 的 managed root 与 `cc/<type>/<slug>` 规则。
- Python 版本：沿用主仓现有 3.11+（`run/__pycache__` 下已并存 cpython-311 与 cpython-313 产物，二者均可）。
- Python 依赖：
  - 现有 `pip install -r run/crawler/requirements-scrapling.txt`
  - 新增 `pip install -r run/crawler/requirements-cloakbrowser.txt`
- CloakBrowser 首跑会自动拉 ~200MB Chromium binary 到用户缓存目录，后续可复用。
- ApexLoL 真实触网；GET 请求不携带任何凭据/代理 token，URL 限定为 `https://apexlol.info` 主域。

## 9. 不变量

- 不修改 `run/scraping/`、`run/processing/`、`run/display/` 任意文件。
- 不动 `run/crawler/scrapling_client.py` / `smoke_scrapling.py` 既有逻辑。
- 不读取或写入凭据、auth、cookie、API key、`.env`、`auth.json` 等敏感文件。
- 不写主仓 commit / push / PR；产物只落 worktree 内 `data/runtime/reports/`。
- 不主动清理 worktree；失败时保留所有产物供分析。

## 10. 风险与开放问题

- **CloakBrowser binary 自动更新**：上游可能在 smoke 之后下发新 binary，结果不可重现；本次接受这个风险，记录 `cloakbrowser.__version__` 入 `result.json` 备查。
- **ApexLoL 反爬版本漂移**：站点可能在 smoke 后升级 CF/Turnstile，结论时效性有限；spec 不承诺长期有效。
- **网络环境差异**：用户本地若处在被 CF 限速的网段，scrapling get 可能假阳性失败；接受这个噪音，由 CloakBrowser 路径互为印证。
- **退出码 2 与退出码 1 区分**：环境缺失需要诊断信息进 stderr.log，便于 Codex 把"装失败"和"被拦"区分清楚。

## 11. 与后续工作的关系

- 本 smoke 不重构 [full_synergy_scraper.py](../../../run/scraping/full_synergy_scraper.py)，但其产物会成为下一步是否启动重构的决策依据。
- `apex_markers.py` 的判定逻辑刻意写得宽松，主要用于本次 smoke；后续真要落业务时由专门的 parser 取代，不沿用。

## 12. 2026-05-27 首跑实测补丁

本节为首跑后基于实测产物（worktree `claudecode-cc-probe-scrapling-apex-smoke` 内 `20260527_094131_scrapling_get`、`20260527_094142_cloakbrowser`）回填的事实修正，覆盖第 6、7 节的对应描述；具体证据见 [smoke_review.md](../../../run/data/runtime/reports/scrapling_apex_smoke/smoke_review.md)。

### 12.1 CF 判定文案扩展

第 7 节原文案 `attention required + cloudflare` 已不匹配 ApexLoL 当前形态。Codex 在 `apex_markers._has_cf_block` 中扩展为：

- 旧形态：`attention required` + `cloudflare`（保留兼容）
- 当前形态：`just a moment` + (`challenges.cloudflare.com` 或 `_cf_chl_opt`)

证据：首跑 scrapling get 抓回的 HTML 含 `<title>Just a moment...</title>` 与 `window._cf_chl_opt`。

### 12.2 origin 不可用判定新增

第 7 节结论矩阵只考虑"拿到 marker"与"被 CF 拦"，未覆盖"穿过反爬层但 origin 失败"的第三类。首跑 CloakBrowser 命中 Vercel `503: SERVICE_UNAVAILABLE / DEPLOYMENT_PAUSED`，按旧矩阵会被错误归为"无 marker"。

补丁：`apex_markers` 后续应新增 `has_origin_unavailable` 标签，识别 Vercel `DEPLOYMENT_PAUSED` 与同类 5xx 暂停页面；本节不强制修改首跑代码，但**结论矩阵新增一行**：

| scrapling get | CloakBrowser | 备注 | 结论 |
|---|---|---|---|
| 1 (CF 拦) | 1 (origin 不可用) | CloakBrowser 穿过 CF 但 origin 挂了 | **INCONCLUSIVE**——反爬层间接验证有效，等 origin 恢复后重跑 |

### 12.3 status_code 语义澄清

`result.json` 的 `status_code` 字段在 CloakBrowser 模式下指 **Playwright 首次 goto response** 的 HTTP 状态码，并非 `page.content()` 最终落地页面状态。首跑示例：

- 首次响应：CF challenge 返回 403 → 写入 `status_code: 403`
- 最终页面：CloakBrowser 解 CF challenge 后跳到 Vercel 503 → HTML 实际是 503 paused

后续判读 result.json 时，**HTML 终态以 `page.html.txt` 的 `<title>` 与 hydration 内容为准**，`status_code` 仅做首跳参考。

### 12.4 首跑实证结论

- **scrapling get** 被 CF managed challenge 拦截，符合预期：裸 HTTP 没有 JS 解 token 能力。
- **CloakBrowser** 穿过 CF 到 origin，**间接证明 patched Chromium 对 ApexLoL 当前 CF 配置有效**；但本轮 origin 处于 Vercel 暂停部署态，未能拿到联动 hydration。
- 本轮 smoke 不能证实最终能爬到联动数据，但**反爬层的判断信息已经取到**：等 ApexLoL 恢复部署后由 Codex 重跑 smoke 即可补足。
- spec 主体（第 1–11 节）的边界与单元设计**未受影响**，本节仅补充事实层修正。
