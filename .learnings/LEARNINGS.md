<!--
中文简介：本文件是 claudecode repo-local 的 tracked learning 记录。
什么时候读：做 self-improvement、复盘重复问题、准备晋升规则时读取。
约束什么：只记录经过人工审查或明确保留价值的 repo-local 经验。
不负责什么：不保存 raw error log，不作为全局 learning，不写入 kb。
-->

## [LRN-20260423-001] best_practice

**Logged**: 2026-04-23T21:35:00+08:00
**Priority**: high
**Status**: pending
**Area**: frontend

### Summary
首页与详情页主展示数值应与排序口径解耦：主展示用真实胜率，排序可继续使用贝叶斯/综合分。

### Details
本轮排查中发现，首页卡片与详情页头部直接展示贝叶斯平滑后的胜率，用户会自然把该值理解为“真实胜率”，从而与外部站点 raw 胜率产生强烈认知冲突。更稳妥的产品设计是：主展示统一使用真实胜率；排序和 tier 分层仍可保留贝叶斯胜率与综合分数，以兼顾用户直觉与低样本去噪。

### Suggested Action
默认在首页卡片、详情页头部和悬浮窗主显示真实胜率；将贝叶斯胜率降级为参考字段或排序字段，并在文案上明确“贝叶斯参考”而不是“真实胜率”。

### Metadata
- Source: conversation
- Related Files: run/display/static/index.html, run/display/static/detail.html, run/display/hextech_ui.py, run/processing/view_adapter.py
- Tags: ranking, winrate, bayesian, frontend, ux
- Pattern-Key: frontend.winrate-display-decoupling

---

## [LRN-20260424-006] workflow_boundary

**Logged**: 2026-04-24T22:20:00+08:00
**Priority**: high
**Status**: pending
**Area**: workflow

### Summary
本仓“云端”口径指 Gemini / Claude / GPT 网页端 AI 验证层，不是 Codex Cloud 的任务派发机制。

### Details
- 网页端 AI 只用于信息检索、想法验证、结果审查和第二视角；不下发任务、不自动执行、不做最终决策。
- CX App / Codex 是跨仓个人助理，有自己的记忆系统；本仓 self-improvement 不干涉 CX 记忆。
- CC for VS Code 仍是本仓主执行者；网页端 AI 输出只能作为 plan / review 输入。
- Self-improvement 可复盘和提议，经用户授权可执行 repo-local CC skill 变更；全局同步需人工审查，不默认改 `kb`。

### Suggested Action
以后修订本仓 workflow / skill / hook 口径时，先检查是否误把网页端 AI 验证层写成 Codex Cloud 任务派发；每次 self-improvement 进化都在本文件追加独立条目。

### Metadata
- Source: workflow boundary correction
- Related Files: AGENTS.md, CLAUDE.md, PROJECT.md, agent_tooling_baseline.md, .claude/tools/learning-loop/README.md
- Tags: workflow, web-ai, self-improvement, cx-memory, cc-skill
- Pattern-Key: workflow.web-ai-validation-not-dispatch

---

## [LRN-20260423-002] best_practice

**Logged**: 2026-04-23T21:35:00+08:00
**Priority**: high
**Status**: pending
**Area**: frontend

### Summary
详情页头部统计不能信任从首页 URL 传入的 wr/bayesWr/pr 参数，应该在页面加载后主动请求最新英雄统计。

### Details
本轮定位到详情页头部的真实胜率、贝叶斯参考和出场率原先直接来自首页跳转时拼接到 URL 中的参数。只要首页页面是旧状态、未正确热更新或使用了旧内存数据，详情页头部就会被旧参数污染，即使详情页正文的海克斯列表已经从最新 API 获取到了新数据。正确做法是让详情页在加载时独立拉取最新英雄榜数据，再根据 heroId 或 heroName 匹配并填充头部统计。

### Suggested Action
详情页仅通过 URL 保留 hero、id、en 等定位信息；头部统计通过页面加载后的 `/api/champions` 请求主动获取，并在 `data_updated` 到来时同步刷新头部。

### Metadata
- Source: conversation
- Related Files: run/display/static/detail.html, run/display/static/index.html, run/display/web_runtime.py
- Tags: detail-page, stale-data, frontend, routing, ws
- Pattern-Key: frontend.detail-header-live-stats

---

## [LRN-20260423-003] best_practice

**Logged**: 2026-04-23T21:35:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
当前项目首页的贝叶斯平滑权重 `0.005` 对低 pick rate 英雄压制过重，调到 `0.001` 更接近用户预期且仍保留去噪作用。

### Details
通过直接比对最新 CSV 与首页榜单计算结果发现，`min_pick_rate = 0.005` 会让低出场率但高真实胜率的英雄被明显拉回均值，例如灵罗娃娃（Gwen）从 raw 55.27% 被压到约 51.69%，用户体感明显不合理。将权重调整到 `0.001` 后，Gwen 的贝叶斯胜率回升到约 53.79%，同时首页整体排序仍保留对低样本噪音的抑制，观感与主流站点更接近。

### Suggested Action
保留贝叶斯平滑作为排序参考，但将首页 `min_pick_rate` 默认设为 `0.001`；如果后续继续调参，优先在 `0.0005 ~ 0.0015` 范围内评估。

### Metadata
- Source: conversation
- Related Files: run/processing/view_adapter.py
- Tags: bayesian, ranking, tuning, backend, statistics
- Pattern-Key: backend.bayesian-weight-too-heavy

---

## [LRN-20260424-004] best_practice

**Logged**: 2026-04-24T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: tooling

### Summary
Tool calls must not include empty arrays or empty required parameters such as `Read(pages=[])`.

### Details
A repeated `Read` call used an empty `pages` parameter, causing invalid/failed tool use and wasting the loop before correction. Empty optional fields should be omitted, and required fields should be checked for valid non-empty values before invoking a tool.

### Suggested Action
Before each tool call, validate required fields and remove empty optional fields. If a parameter cannot be populated with a valid value, pause to inspect context instead of sending an empty placeholder.

### Metadata
- Source: conversation
- Related Files: .learnings/ERRORS.md
- Tags: tool-use, validation, read, empty-parameters
- Pattern-Key: tooling.empty-tool-parameter-guard

---

## [LRN-20260424-005] best_practice

**Logged**: 2026-04-24T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: workflow

### Summary
本仓工作流控制面默认先确认工作区、中文输出，并在已批准计划后避免二次审批门。

### Details
- 已批准计划或 ready signal 后，不把 intent frame 当第二审批门；除非出现 blocker、越界或危险操作，继续执行最小安全实现。
- 面向用户输出默认中文；必要英文路径、命令、API、配置键、模型名、Git branch、review verdict 标签保留原文。
- 工作区不明确时先只读列候选，不把仓库根目录当默认业务写入区。
- 不要使用空工具参数，例如 `Read(pages=[])`；可省略的空参数直接省略。

### Suggested Action
执行任务前声明 `target_work_area` 和 `allowed_write_scope`；输出结论先用中文，再附必要英文证据。

### Metadata
- Source: workflow control-plane patch
- Related Files: AGENTS.md, CLAUDE.md, work_area_registry.md
- Tags: work-area, plan-mode, chinese-output, tool-use
- Pattern-Key: workflow.control-plane-defaults

---

## [LRN-20260426-001] best_practice

**Logged**: 2026-04-26T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: tooling

### Summary
Windows native Python 不应直接使用 `/c/Users/...` 这类 Bash 风格路径。

### Details
在本仓 Windows 环境中，传给 native Python、`pathlib`、PowerShell 或文件工具参数的路径如果使用 `/c/Users/...`，可能被解析成 `\c\Users\...` 并触发 `FileNotFoundError`。Bash 风格路径只适合 shell 层；跨到 Windows-native 工具时，需要使用 `C:/Users/...`、工具原生绝对路径，或先做路径归一化。

### Suggested Action
编写 Windows-native Python/PowerShell 辅助命令或工具参数时，优先使用 Windows 可解析绝对路径；不要把 Git Bash/MSYS 路径原样传入 native Python。

### Metadata
- Source: self-improvement-promotion review of .learnings/ERRORS.md
- Related Files: .learnings/ERRORS.md
- Tags: windows, python, paths, tooling
- Pattern-Key: tooling.windows-native-paths

---

## [LRN-20260426-002] workflow_boundary

**Logged**: 2026-04-26T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: workflow

### Summary
严格只读任务中应避免主动运行预期可能失败的 Bash 命令。

### Details
本仓配置了 `PostToolUseFailure` 的 Bash failure hook；失败的 Bash 调用会写入 `.learnings/ERRORS.md`。因此，当用户明确要求 strict read-only audit、不得写 `ERRORS`、不得修改文件或类似约束时，主动运行可能失败的 Bash 命令会把“只读探索”变成写入 raw log 的副作用。

### Suggested Action
严格只读任务优先使用 Read、Grep、Glob 等不会触发 Bash failure log 的工具；如果必须运行可能失败的 Bash，先说明它可能写入 `.learnings/ERRORS.md` 并取得用户确认。

### Metadata
- Source: self-improvement-promotion review of strict read-only audit risk
- Related Files: .claude/hooks/self-improvement-error-log.sh, .learnings/ERRORS.md
- Tags: read-only, bash, hooks, errors, workflow
- Pattern-Key: workflow.strict-readonly-bash-failure-hook

---

## [LRN-20260426-003] best_practice

**Logged**: 2026-04-26T00:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: tooling

### Summary
Windows 控制台输出中文、替换字符或大段文本时要显式控制编码和输出范围。

### Details
在本仓 Windows 环境中，用 Python 暴力打印中文、替换字符或大型 Markdown 内容时，默认 GBK 控制台可能触发 `UnicodeEncodeError`，并让本来只需读取片段的审查变成失败命令。优先使用专用读取工具或分段查询；必须用 Python 输出时，显式设置 UTF-8 输出或限制为 ASCII-safe 摘要。

### Suggested Action
不要用 Python 一次性打印大型中文文件；优先使用 Read/Grep 分段读取。确需脚本输出时，设置 UTF-8 输出并控制输出长度。

### Metadata
- Source: self-improvement-promotion review of .learnings/ERRORS.md
- Related Files: .learnings/ERRORS.md
- Tags: windows, encoding, python, unicode, tooling
- Pattern-Key: tooling.windows-console-utf8-bounded-output

---
