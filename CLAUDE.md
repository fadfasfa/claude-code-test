# claudecode

`claudecode` 是一个多工作区开发仓库。它包含仓库治理文件，也包含 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/` 等独立 coding / data 工作区。

仓库根目录是工作流入口和任务路由层，不是默认业务实现目录。

## 当前执行角色

- Claude Code 是本仓日常主力开发端，负责开发、验证和本地执行闭环。
- Codex 用于全局 / 跨仓库能力治理、重构辅助、个人助理型任务和云端 PR 审查；它不是本仓任务派发的必经环节。
- Web 端 AI 只用于需求收敛、知识搜集和意见参考，不下发任务、不替代最终决策。
- Antigravity 仅人工显式触发，用于 Claude Opus 4.6 独立审查或 Gemini 高难前端落地；本仓不启用 Antigravity Gate。
- `.ai_workflow` 已退役，不恢复；不启用 `finish-task`、`event_log`、`active_tasks_index`。
- `planning-with-files` 保留为规划辅助 skill / plugin，不是强制状态机，也不是任务台账。

## Claude Code 读取顺序

1. `CLAUDE.md`
2. `AGENTS.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. 相关 `docs/*.md`

入口文件不是每次都要全量读取。普通任务优先读取中文头部简介、目录、目标工作区相关段落和相关小节；已知 `target_work_area` 时，只搜索该工作区相关段落。不要默认读取 `AGENTS.md`、`PROJECT.md` 或 `work_area_registry.md` 的 1-2000 行，也不要把完整入口链全文件读取当作启动固定动作。

### Native Read limits for text/code files

当前本仓已知 Claude Code 在 Codex Proxy / GPT 模型路径下，原生 `Read` 对 text/code/Markdown 文件可能自动携带 `pages: ""` 或错误 `pages` 参数并失败。因此默认不把原生 `Read` 当作 Markdown/text/code 探索入口，只保留明确的 Edit 前置登记例外：

- Markdown/text/code 文件默认不要用原生 `Read` 做大范围阅读，不得传 `pages` 参数；优先用 `rg`、`Get-Content`、Python UTF-8 line-number preview、`Grep` / `Glob` / scoped `Bash` 等窄范围读取。
- 当下一步需要使用 `Edit` 修改某个 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 目标文件时，允许对该目标文件执行一次原生 `Read` 作为 `Edit` 前置登记；许可只针对即将被 `Edit` 的目标文件，不得借此读取整批 Markdown/text 文件或扩大探索范围。
- 不得给 Markdown/text/code 文件的原生 `Read` 传 PDF `pages` 参数；PDF 才允许页范围语义，Markdown/text/code 只允许路径与行范围/片段读取。
- 如果主线程或 subagent 已经发生一次 `Read` `pages` / `schema` / unsupported parameter / malformed input / `Invalid pages parameter` 失败，不得重试同文件同类 `Read`，不得声称“这次不传 pages”后再次发起同类 `Read`；上面的 Edit 前置登记例外不适用于该失败场景。
- 源码和文档探索优先使用 `.claude/agents/repo-explorer.md`，或直接用 `Grep` / `Glob` / `Bash` 获取少量片段。
- `repo-explorer` 只能使用 `Grep` / `Glob` / `Bash`。
- 需要上下文时，用 `Grep` / `Glob` / `Bash` 获取片段或让 `repo-explorer` 汇总；非 Edit 前置登记场景如果仍需要完整上下文，停止并报告 blocker。
- 在 Windows / Claude Bash 中用 Python heredoc 或 one-liner 打印仓库 Markdown/text 文件时，使用 `PYTHONIOENCODING=utf-8 python -X utf8` 或等价环境变量设置；Python 内部先执行 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；读取 Markdown/text 时使用 `encoding="utf-8-sig", errors="replace"` 后再打印，避免 GBK 输出遇到 BOM 或特殊字符时报 `UnicodeEncodeError`。
- 对 `.learnings`、日志和源码片段做摘要时，限制输出范围，优先输出标题、首行错误、dedupe、timestamp 等字段；遇到乱码、替换字符或长异常 payload 时使用 `errors="replace"` 或 ASCII-safe 摘要，不原样倾倒全文。
- 如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而不可用，先按上方目标文件登记例外对即将 `Edit` 的目标文件执行一次原生 `Read`；若已出现 `pages` / `schema` / malformed / `Invalid pages parameter` 失败，则不得再用该例外，改用 scoped shell/Python 只读确认后报告 blocker 或等待授权。不得临时用 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 绕过。
- 只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才允许用脚本化补丁修改该文件；执行前必须只读确认目标字符串或块唯一匹配，输出匹配数量，匹配不是 1 就停止，不扩大替换范围或猜测邻近片段。
- `full_synergy_scraper.py` 这类源码文件不能“重试读取”；首次遇到 `Read` 参数失败后，同文件原生 `Read` 路径立即关闭。

只读探索默认使用 `.claude/agents/repo-explorer.md`。不要用 built-in `Explore` 承担需要中文 Todo、原生 `Read` 禁令或路径纪律的任务；本仓 repo-local hook 会阻断 `Agent(Explore)` 并要求改用 `repo-explorer`。

`.claude/` 存放 repo-local settings、skills、hooks 和 tools。它不能替代根目录入口文件。

## 默认流程

- 小任务：确认目标工作区，读取窄范围上下文，修改，运行最近验证，报告。
- 中任务：写简短计划，必要时确认假设，在窄范围内分步修改，验证，报告。
- 大任务：收敛需求，拆分工作，判断是否需要 worktree，判断是否需要 TDD，可选使用 subagents 做边界清晰的旁路工作，最后做 PR-style review。
- 小任务不需要 `agents.md`；本仓不创建 lowercase `agents.md`。
- 对大任务，进入详细计划前可先做轻量 brainstorm / 方案比较，用于收敛方向；brainstorm 本身不得替代验收计划、任务拆分或验证。

详细决策表见 `docs/task-routing.md`。

## 边界

- 本仓不继承 `kb` 的 ingest/wiki 工作流。
- 不得从本仓工作流修改 `C:\Users\apple\kb`。
- 不得从普通业务任务修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App、Codex Proxy、全局 hooks、全局 skills 或全局 AGENTS/CLAUDE 文件；只有用户明确发起全局层治理任务时才可进入。
- 不要创建项目级 `.codex/config.toml`、`.mcp.json`、`playwright-mcp/` 或其他 MCP 目录。
- 不启用完整 Superpowers SessionStart，也不启用 ECC。

## 工作区规则

业务实现前，使用 `work_area_registry.md` 声明 `target_work_area`。

如果任务只修改仓库治理文件，目标工作区是 `repo-root-governance`。如果目标不清楚，保持只读并列出候选项。

## 连续执行

对已接受的多步计划，只把 `.tmp/active-task/current.md` 当作运行态 ledger。它已被 ignored，不是 learning，不是规则来源，也不能授权危险操作。

Plan Mode 审批通过 ExitPlanMode 或对话提交完成；不要默认把计划写到 `C:\Users\apple\.claude\plans\*.md`。如确实需要计划落盘，只能写 repo 内 `.tmp/active-task/current.md`；全局 `.claude\plans` 写入必须由用户显式要求。

`planning-with-files` 只用于复杂、多步骤、容易遗忘的任务。`task_plan.md`、`findings.md`、`progress.md` 是本地运行态工作记忆，默认不提交；`task_plan.md` 只写目标、阶段、状态和已确认决策，外部网页、搜索结果、模型输出、未验证材料只能写入 `findings.md`，不能写入 `task_plan.md`。这些计划文件不能作为危险操作授权凭证；dangerous git / worktree 操作仍必须遵守现有 worktree-governor、safety-boundaries 和 permission deny 规则。`planning-with-files` 不替代 `.tmp/active-task/current.md`，也不启用旧 Hextech 状态机；如两者并存，active-task 仍只是运行态摘要，不是授权源。

已批准范围内的安全步骤可以继续执行。遇到 blocker、范围变化、dirty-tree 范围不清、危险 git 操作、global/kb 边界风险、依赖安装或用户意图不清时，必须停下。

如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而失败，先按 Native Read limits 中的目标文件登记例外补一次目标文件原生 `Read`；若已出现 `pages` / `schema` / malformed / `Invalid pages parameter` 失败，不得再补同类 `Read`。如果 `Edit` 仍失败，必须停止并报告具体失败原因。不得退到 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 修改业务文件。只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才能继续。

## 常用入口文档

优先用内置 `Grep` / `Glob` / `Bash` 定位 `work_area_registry.md`、`docs/task-routing.md`、`docs/safety-boundaries.md` 和 `docs/continuous-execution.md` 中的相关小节；非 Edit 前置登记场景如果需要全文上下文但 text/code 文件不能安全使用原生 `Read`，报告 blocker。Git 状态仍可用 `git status --short --branch` 做只读确认。
