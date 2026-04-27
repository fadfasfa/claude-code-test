# claudecode

`claudecode` 是一个多工作区开发仓库。它包含仓库治理文件，也包含 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/` 等独立 coding / data 工作区。

仓库根目录是工作流入口和任务路由层，不是默认业务实现目录。

## Claude Code 读取顺序

1. `CLAUDE.md`
2. `AGENTS.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. 相关 `docs/*.md`

入口文件不是每次都要全量读取。普通任务优先读取中文头部简介、目录、目标工作区相关段落和相关小节；已知 `target_work_area` 时，只搜索该工作区相关段落。不要默认读取 `AGENTS.md`、`PROJECT.md` 或 `work_area_registry.md` 的 1-2000 行，也不要把完整入口链全文件读取当作启动固定动作。

### Native Read ban for text/code files

当前本仓已知 Claude Code 原生 `Read` 对 text/code 文件可能自动携带 `pages` 参数并失败。因此：

- 不要在主线程或 subagent 中对 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 文件使用原生 `Read`。
- 源码和文档探索优先使用 `.claude/agents/repo-explorer.md`，或直接用 `Grep` / `Glob` / `Bash` 获取少量片段。
- `repo-explorer` 只能使用 `Grep` / `Glob` / `Bash`。
- 如果主线程或 subagent 已经发生一次 `Read` `pages` / unsupported parameter / malformed input 失败，不得重试同文件 `Read`，不得声称“这次不传 pages”后再次发起同类 `Read`。
- 需要上下文时，用 `Grep` / `Glob` / `Bash` 获取片段或让 `repo-explorer` 汇总；如果仍需要完整上下文，停止并报告 blocker。
- 如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而不可用，停止并报告 blocker；不得临时用 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 绕过。
- 只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才允许用脚本化补丁修改该文件。
- `full_synergy_scraper.py` 这类源码文件不能“重试读取”；首次遇到 `Read` 参数失败后，同文件原生 `Read` 路径立即关闭。

只读探索默认使用 `.claude/agents/repo-explorer.md`。不要用 built-in `Explore` 承担需要中文 Todo、原生 `Read` 禁令或路径纪律的任务；本仓 repo-local hook 会阻断 `Agent(Explore)` 并要求改用 `repo-explorer`。

`.claude/` 存放 repo-local settings、skills、hooks 和 tools。它不能替代根目录入口文件。

## 默认流程

- 小任务：确认目标工作区，读取窄范围上下文，修改，运行最近验证，报告。
- 中任务：写简短计划，必要时确认假设，在窄范围内分步修改，验证，报告。
- 大任务：收敛需求，拆分工作，判断是否需要 worktree，判断是否需要 TDD，可选使用 subagents 做边界清晰的旁路工作，最后做 PR-style review。
- 对大任务，进入详细计划前可先做轻量 brainstorm / 方案比较，用于收敛方向；brainstorm 本身不得替代验收计划、任务拆分或验证。

详细决策表见 `docs/task-routing.md`。

## 边界

- 本仓不继承 `kb` 的 ingest/wiki 工作流。
- 不得从本仓工作流修改 `C:\Users\apple\kb`。
- 不得从本仓工作流修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App、Codex Proxy、全局 hooks、全局 skills 或全局 AGENTS/CLAUDE 文件。
- 不要创建项目级 `.codex/config.toml`、`.mcp.json`、`playwright-mcp/` 或其他 MCP 目录。
- 不启用完整 Superpowers SessionStart，也不启用 ECC。

## 工作区规则

业务实现前，使用 `work_area_registry.md` 声明 `target_work_area`。

如果任务只修改仓库治理文件，目标工作区是 `repo-root-governance`。如果目标不清楚，保持只读并列出候选项。

## 连续执行

对已接受的多步计划，只把 `.tmp/active-task/current.md` 当作运行态 ledger。它已被 ignored，不是 learning，不是规则来源，也不能授权危险操作。

Plan Mode 审批通过 ExitPlanMode 或对话提交完成；不要默认把计划写到 `C:\Users\apple\.claude\plans\*.md`。如确实需要计划落盘，只能写 repo 内 `.tmp/active-task/current.md`；全局 `.claude\plans` 写入必须由用户显式要求。

已批准范围内的安全步骤可以继续执行。遇到 blocker、范围变化、dirty-tree 范围不清、危险 git 操作、global/kb 边界风险、依赖安装或用户意图不清时，必须停下。

如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而失败，必须停止并报告 blocker；不得退到 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 修改业务文件。只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才能继续。

## 常用入口文档

优先用内置 `Grep` / `Glob` / `Bash` 定位 `work_area_registry.md`、`docs/task-routing.md`、`docs/safety-boundaries.md` 和 `docs/continuous-execution.md` 中的相关小节；需要全文上下文但 text/code 文件不能安全使用原生 `Read` 时，报告 blocker。Git 状态仍可用 `git status --short --branch` 做只读确认。
