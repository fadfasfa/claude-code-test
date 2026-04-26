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
  - `WorktreeCreate`：worktree 命名护栏
  - `PreToolUse`：裸 shell/worktree 危险命令拦截
  - `PostToolUseFailure`：self-improvement raw error 捕获
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

## Worktree

- 详细策略：`docs/git-worktree-policy.md`
- worktree helper：`.claude/tools/worktree-governor/new_worktree.ps1`
- 创建 worktree 需要 purpose 和 owner。不带 `-DryRun` 真实创建前，必须有明确用户指令。
- 删除 worktree 永远需要人工确认。
- 旧 nested `.claude/worktrees/**` 不是当前工作流来源，也不得作为新工作的模板。

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

Superpowers 不作为本仓默认 SessionStart 工作流。Superpowers/TDD 只在完成模块准入并获得用户确认后，作为任务级候选路线。

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

Raw logs 和 ledgers 都不是规则，不能自动晋升。`ERRORS → LEARNINGS` 候选精炼使用 `/maintenance learning`，并且在修改 tracked learning 前需要用户审查清单。

稳定 `LEARNINGS → docs / skills / entry` 的第二阶段晋升审查使用 `/promote-learning`，默认只输出 patch plan。

Self-improvement 不得从本仓工作流修改 `kb` 或全局层。

## Codex

- Codex 读取仓库入口文档和 workspace 文档。
- 不创建项目级 `.codex/config.toml`。
- 仓库层保持 plugins = 0、MCP = 0、Codex hooks = 0。
- Codex 不自动创建 worktree 或分支。
- 未经明确人工确认，Codex 不执行 push、PR、merge、rebase、stash、reset、clean 或 remove worktree。
