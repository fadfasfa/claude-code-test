# Agent Tooling Baseline

本文件记录 `claudecode` 的 repo-local 能力基线。它不是全局真相源，也不得用于修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App 或 Codex Proxy 配置。

## 范围

- 仓库范围：`C:\Users\apple\claudecode`
- 只读边界参考：`C:\Users\apple\kb`
- 除非用户另起全局任务，否则本仓工作流禁止修改：`C:\Users\apple\.claude`、`C:\Users\apple\.codex`、全局 hooks、全局 skills、全局 AGENTS/CLAUDE 文件、CLI 安装、VS 插件、Codex App、Codex Proxy、全局 Superpowers/ECC 安装。

## Claude Code

- 仓库入口：`C:\Users\apple\claudecode\CLAUDE.md`
- 仓库 settings：`.claude/settings.json`
- 当前通过 settings 启用的 repo hooks：
  - `WorktreeCreate`：worktree owner 判定、命名护栏和 repo 外 registry marker 写入
  - `WorktreeRemove`：只清理 clean `owner=agent` ephemeral worktree，保留 branch
  - `PreToolUse`：裸 shell/worktree 危险命令拦截；阻断 built-in `Explore`，要求改用 `repo-explorer`
- Optional debug hook：`.claude/hooks/self-improvement-error-log.sh` 可用于临时捕获 Bash 失败到 ignored raw cache；默认不在 `.claude/settings.json` 注册，避免只读任务产生额外写入噪音。
- Disabled / experimental hook：`.claude/hooks/block-read-pages-for-text.ps1` 只保留为禁用状态说明，不在 settings 注册，也不再通过 `updatedInput` 修正 `Read` 的 `pages` 参数。
- Native Read ban for text/code files：本仓主线程和 subagent 不对 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 使用原生 `Read`。业务修改前先用内置 `Grep` / `Glob` / `Bash` 做只读定位；需要源码或文档汇总时使用 `.claude/agents/repo-explorer.md`。
- 如果已经发生一次 `Read` `pages` / unsupported parameter / malformed input 失败，不重试同文件原生 `Read`，也不得声称“这次不传 pages”后再次发起同类 `Read`；`full_synergy_scraper.py` 这类源码文件不能“重试读取”。如果仍需要全文上下文，必须报告 blocker。不要因原生 `Read` / `Edit` 失败退到 PowerShell 直接改业务文件；只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才允许脚本化补丁。
- 只读探索默认使用 `.claude/agents/repo-explorer.md`；不要用 built-in `Explore` 承担需要中文 Todo、原生 `Read` 禁令或路径纪律的任务。`repo-explorer` 不暴露 Read，只用 Grep / Glob / Bash 避开 text/code `Read` 被自动附加 `pages` 的工具链问题。
- Hooks 必须保持为安全机制或轻量提醒机制。它们不能变成调度器、自动继续引擎或业务文件写入器。
- 仓库 env 使用 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` 和 `CLAUDE_CODE_FORK_SUBAGENT=0` 禁用后台 / fork worktree 派生。
- 只读 Explore / review subagent 不是 worktree 触发条件；只有明确隔离执行或已接受计划批准 worktree 时，才允许进入 `WorktreeCreate`。

## Repo Skills

现有 repo-local skills：

- `api-design`
- `backend-patterns`
- `frontend-patterns`
- `python-patterns`
- `python-testing`
- `requesting-code-review`
- `verification-before-completion`

本仓新增的工作流入口 skills：

- `resume-active-task`
- `module-admission`
- `frontend-polish-lite`
- `maintenance`
- `promote-learning`
- `review-diff`

这些 skills 是按需工作流入口。它们不是自动 SessionStart 注入，也不被 `kb` 继承。

## 任务重量

- 小任务：不强制计划、TDD、worktree、subagent 或 PR review。
- 中任务：需要简短计划和验证；只有风险足够时才启用更重工具。
- 大任务：需求收敛、轻量 brainstorm / 方案比较、任务拆分、可选 worktree、可选 TDD、可选 subagent 并行和 PR-style review。

详细路由见 `docs/task-routing.md`。

## Methodology Commands

- 方法策略文档：`docs/agent-methodology-policy.md`。
- Superpowers 当前不全量启用；未来试装也只允许 local scope / 单仓试验，不允许 user/global 默认启用，不启用 SessionStart hook 自动注入，不默认启用插件 `code-reviewer`。
- Slash command：`/brainstorm-task` 只做需求澄清、方案比较和风险列举；默认不改文件、不创建分支、不启动 worktree。
- Slash command：`/tdd-task` 只用于明确开发 / 修 bug / 高风险行为变更；先定位现有测试体系，没有测试体系时只提出最小测试方案，不为文档、规则、小脚本维护强行 TDD。
- Slash command：`/review-pr-local` 调用 `.claude/agents/local-pr-reviewer.md` 做本地只读 PR 审查，审查当前分支相对 base 的 git diff，替代云端 Codex PR Review。
- Review helper：`.claude/tools/pr/review_local_pr.ps1` 只收集 `git status --short`、`git rev-parse --abbrev-ref HEAD`、`git diff --stat <base>...HEAD`、`git diff --name-status <base>...HEAD`、`git diff <base>...HEAD` 和 `git log --oneline <base>..HEAD`。
- `/review-pr-local` 不运行 `gh pr review --submit`、`gh pr merge` 或 `git push`，不提交，不改代码；可选报告只能写入 ignored `.tmp/pr-review/<branch>.md`。

## Worktree

- 详细策略：`docs/git-worktree-policy.md`
- worktree helper：`.claude/tools/worktree-governor/new_worktree.ps1`
- authorized scan/cleanup wrapper：`.claude/tools/worktree-governor/scan_agent_worktrees.ps1`
- 用户显式长期 worktree 只认 helper `-Owner directed`，或 WorktreeCreate name 显式带 `directed-` / `user-`。
- 普通 Agent / Explore / review / subagent / `isolation: "worktree"` 自动创建的 worktree 一律视为 `owner=agent` ephemeral，并写入 `C:\Users\apple\_worktrees\claudecode\.registry\*.json`。
- `WorktreeRemove` 只允许对 clean `owner=agent`、`protected=false`、位于 auto root 的 worktree 执行非 force `git worktree remove <path>`；dirty 时只报告 blocker；branch 不自动删除。
- `owner=user` 或 `protected=true` 的 persistent worktree 永远跳过自动清理。
- agent branch sweep helper：`.claude/tools/worktree-governor/sweep_agent_branches.ps1` 是 lower-level legacy/manual helper，不作为常规入口。
- Slash command：`/scan-agent-worktrees` 是唯一 agent worktree / branch 清理入口。用户调用它即授权通过受控 wrapper 扫描、删除 clean agent auto worktree，并删除 zero-ahead `wt-auto-*` agent branch；中途不再追加 PowerShell / Bash 确认。dirty、`owner=user`、`protected=true`、非 auto root、无 marker、unique commits、仍被 checkout、pushed/upstream branch 只报告，不自动删除。
- `/cleanup-agent-worktrees` 不再作为常规入口。
- 旧 nested `.claude/worktrees/**` 不是当前工作流来源，也不得作为新工作的模板。
- `scripts/git/ccw-*` 当前是 legacy/manual tooling，不进入自动 hook contract。

## Task PR Shipping

- Slash command：`/ship-task-pr` 是显式远端写入入口，用于把当前任务改动整理成语义化分支、commit、`git push -u origin <branch>` 和 GitHub PR。
- Wrapper：`.claude/tools/pr/ship_task_pr.ps1`。用户调用 `/ship-task-pr` 本身视为本次 push / PR 的明确授权，但只授权该受控 wrapper。
- `/scan-agent-worktrees` 只承载 agent worktree / branch 受控清理语义；不得复用它承载 push、PR 或远端写入。
- 裸 `git push` 仍不默认放开；本地权限只允许精确 wrapper 命令，不允许 `PowerShell(*)`、`powershell.exe *`、`pwsh *`、`git *` 或 `git push*`。
- wrapper 禁止 force push、push `main` / `master`、删除 branch/worktree、`git reset --hard`、`git clean`，并拒绝提交 `.claude/settings.local.json`、`.tmp/**`、日志文件、`node_modules/**` 和 `.venv/**`。
- PR body 生成到 ignored `.tmp/pr/<branch>/body.md`，不纳入提交。

## Subagents

Subagents 只允许用于边界清晰的旁路工作：

- 只读探索
- 测试入口发现
- 失败归因
- review
- 明确计划中、文件所有权互不重叠的窄范围实现

不要让并发 subagents 编辑同一文件、共享 settings、hooks、worktree policy 或共享数据契约。

只读 Explore / review subagent 默认不创建 worktree；worktree hook 失败时只能降级为主线程只读搜索或报告 blocker，不能绕过 hook。

## 外部 AI 验证层

本仓所说云端 / 外部 AI 验证层，指 Gemini / Claude / GPT 网页端或外部模型用于审查、对照和需求收敛；不是 Codex Cloud 任务派发机制。不要把网页端 AI 验证误写成 Codex Cloud 执行流。

## TDD 与 Superpowers

TDD 是 coding-only 选项，不是默认规则。只在高风险行为变更、容易回归的模块，或测试是最清晰控制面的较大任务中使用。

Superpowers 不作为本仓默认 SessionStart 工作流，也不全量安装。Brainstorm、TDD 和 PR Review 先采用本仓轻量命令与本地只读 agent。

## Playwright 与前端

Playwright CLI 只属于 claudecode，且只用于 coding-only 前端验证。它不是 global core，不属于 `kb`，不是全局 hook，也不是所有任务的默认步骤。

当前机器发现 PATH 上有 Playwright CLI，但本仓不得在没有模块准入卡的情况下安装依赖、添加 Playwright config 或添加前端验证脚本。

只有前端任务需要 UI 交互、截图、响应式、视觉或可访问性检查时，才使用 `frontend-polish-lite`。

## ECC

ECC 已剔除；不得作为默认能力或候选模块恢复。未来重新引入必须重新走模块准入卡。

## 网页端 AI 验证层

- Gemini / Claude / GPT 网页端 AI 只用于信息检索、想法验证、结果审查和第二视角。
- 网页端 AI 不是 Codex Cloud 任务派发机制，不下发任务、不自动执行，也不替代本仓最终决策。
- CC for VS Code 仍是本仓主执行者；网页端 AI 输出只能作为 plan / review 输入。

## Self-Improvement

- tracked repo learning：`.learnings/LEARNINGS.md`
- ignored raw error input：`.learnings/ERRORS.md`
- runtime task ledger：`.tmp/active-task/current.md`
- plan approval：通过 ExitPlanMode 或对话提交；不要默认写入 `C:\Users\apple\.claude\plans\*.md`

Raw logs 和 ledgers 都不是规则，不能自动晋升。`ERRORS → LEARNINGS` 候选精炼使用 `/maintenance learning`，并且在修改 tracked learning 前需要用户审查清单。

稳定 `LEARNINGS → docs / skills / entry` 的第二阶段晋升审查使用 `/promote-learning`，默认只输出 patch plan。

Self-improvement 不得从本仓工作流修改 `kb` 或全局层。

## Codex

- Codex 读取仓库入口文档和 workspace 文档。
- 不创建项目级 `.codex/config.toml`。
- 仓库层保持 plugins = 0、MCP = 0、Codex hooks = 0。
- Codex 不自动创建 worktree 或分支。
- 未经明确人工确认，Codex 不执行 push、PR、merge、rebase、stash、reset、clean 或 remove worktree。
