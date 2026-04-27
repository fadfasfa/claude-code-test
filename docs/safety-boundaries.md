# 安全边界

本文件定义 `claudecode` repo-local 工作的边界。

## 仓库范围

本工作流允许写入：

- `C:\Users\apple\claudecode\**`

只有明确需要时，才可只读参考：

- `C:\Users\apple\kb\**`
- 全局 Claude Code / Codex 配置路径

除非用户另起单独任务，否则本仓工作流禁止修改：

- `C:\Users\apple\.claude\**`
- `C:\Users\apple\.codex\**`
- `C:\Users\apple\kb\**`
- CLI install/uninstall/upgrade
- VS plugins
- Codex App
- Codex Proxy
- global Superpowers / ECC installation
- global skills
- global hooks
- global AGENTS / CLAUDE files

当前 repo 任务不得默认写 `C:\Users\apple\.claude\plans\*.md`。计划审批通过 ExitPlanMode 或对话提交完成；如需计划落盘，只能写 repo 内 `.tmp/active-task/current.md`。全局 `.claude\plans` 写入必须由用户显式要求，且不能把 PowerShell `Set-Content` 当作默认 fallback。

## 工作区边界

业务实现前，从 `work_area_registry.md` 选择 `target_work_area`。

如果任务是 repo governance，使用 `repo-root-governance`。

如果目标不清楚：

1. 保持只读。
2. 列出候选工作区。
3. 只有无法做安全假设时才询问方向。

## Dirty Tree 边界

当前仓库可能包含无关用户改动。不得 revert、reset、stash、clean 或覆盖这些改动。

提交或暂存前，先按用途分组 diff，并确认当前任务的精确文件范围。

## 读取边界

避免一次性读取超大范围。优先使用内置 `Grep` / `Glob` / `Bash` 确认候选位置和少量上下文。

只读探索默认使用 `.claude/agents/repo-explorer.md`。不要用 built-in `Explore` 承担需要中文 Todo、原生 `Read` 禁令或路径纪律的任务；`Agent(Explore)` 会被 repo-local PreToolUse hook 阻断。

### Native Read ban for text/code files

当前本仓已知 Claude Code 原生 `Read` 对 text/code 文件可能自动携带 `pages` 参数并失败。因此：

- 不要在主线程或 subagent 中对 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 文件使用原生 `Read`。
- 源码和文档探索优先使用 `.claude/agents/repo-explorer.md`，或直接用 `Grep` / `Glob` / `Bash` 获取少量片段。
- `repo-explorer` 只能使用 `Grep` / `Glob` / `Bash`。
- 如果主线程或 subagent 已经发生一次 `Read` `pages` / unsupported parameter / malformed input 失败，不得重试同文件 `Read`。
- 不得声称“这次不传 pages”后再次发起同类 `Read`。
- 需要上下文时，用 `Grep` / `Glob` / `Bash` 获取片段或让 `repo-explorer` 汇总。
- 如果仍需要完整上下文，停止并报告 blocker；不得发明脚本读取或写入绕过。
- `full_synergy_scraper.py` 这类源码文件不能“重试读取”；首次遇到 `Read` 参数失败后，同文件原生 `Read` 路径立即关闭。

### Edit safety after missing Read registration

如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而失败：

- Do not use PowerShell `Set-Content`.
- Do not use `[System.IO.File]::WriteAllText`.
- Do not use ad-hoc replacement scripts.
- Stop and report blocker.
- Continue only after the user explicitly replies: `授权 scripted patch plan 修改 <file>`.

### Retry budget

- Native `Read` budget for text/code files is zero in the main thread and subagents.
- If a `Read` `pages` / unsupported parameter / malformed input failure has already happened, do not retry native `Read` for the same file.
- Use built-in `Grep` / `Glob` / `Bash` or stop with a blocker.

对大型工作区先搜索关键词和目录结构，不全量读文件。已知 `target_work_area` 时，先检索 `work_area_registry.md` 中该工作区条目，再列工作区一级目录；不要把 `Glob <work_area>/**/*` 作为第一步。只有明确需要更多候选文件时，才扩大 Glob 或跨子区搜索。

严格只读验收或 smoke test 中，不主动执行预期会失败的 Bash，也不通过危险失败试探来证明 hook 生效。

## Windows tooling 边界

- Windows native Python / PowerShell 不应直接使用 `/c/Users/...` Bash 风格路径。
- 进入 native Python、PowerShell 或文件工具参数时，使用 `C:/Users/...` 或 Windows 可解析路径。
- Grep / Glob 优先使用相对路径或 Windows 路径；Bash 中优先使用相对路径，不混用 `/mnt/c` 与 `/c/Users`。
- 中文、替换字符、大段 Markdown 输出在 Windows 控制台可能触发编码问题。
- 优先用内置 `Grep` / `Glob` / `Bash` 分段定位 text/code 文件；必须用 Python 输出时显式控制 UTF-8 或限制输出范围。
- 不用 Python 暴力打印大型中文文件。

## Git 确认边界

`git add` 和 `git commit` 只允许在已接受计划内执行，并且必须有明确用户授权和清晰 diff 范围。

以下操作前必须询问：

- `git push`
- PR creation
- `git merge`
- `git reset`
- `git clean`
- `git rebase`
- `git stash`
- manual `git worktree remove`

唯一例外：用户显式调用 `/ship-task-pr` 时，该调用本身视为本次 `git push -u origin <branch>` 和 `gh pr create` 的明确授权，但只能通过 `.claude\tools\pr\ship_task_pr.ps1` wrapper 执行。裸 `git push` 仍不默认放开，`main` / `master`、force push、delete push、mirror/all push 仍禁止。

另一个例外：用户显式调用 `/scan-agent-worktrees` 时，该调用本身视为清理 clean agent worktree 和 zero-ahead `wt-auto-*` agent branch 的明确授权，但只能通过 `.claude\tools\worktree-governor\scan_agent_worktrees.ps1` wrapper 执行。裸 `git worktree remove` / `git branch -d` 不作为默认路径；force worktree remove、branch 强删、reset、clean、文件系统删除命令仍必须审核或拒绝。

`/ship-task-pr` 还必须拒绝把 `.claude/settings.local.json`、`.tmp/**`、日志文件、`node_modules/**` 或 `.venv/**` 纳入提交，并且不得执行 `git reset --hard`、`git clean`、删除 branch 或删除 worktree。

## Hook 边界

Repo hooks 只能是：

- safety blocks
- naming guards
- lightweight reminders
- raw failure logging

Hooks 不得：

- 调度任务
- 自动继续执行
- 修改业务文件
- 安装依赖
- 修改全局配置
- 变成复杂 workflow engine

只读 Explore / 审查 agent 不得默认创建 persistent/user worktree。普通 Agent / Explore / review / subagent / `isolation: "worktree"` 自动创建的 worktree 一律视为 `owner=agent` ephemeral；只有 helper `-Owner directed` 或 name 显式带 `directed-` / `user-` 才是 persistent/user worktree。

如果 worktree hook 失败，不得绕过 hook 手动创建 worktree；只能降级为主线程只读搜索，或报告需要修复 hook 的 blocker。

`WorktreeRemove` hook 只允许基于 repo 外 registry 清理 clean `owner=agent` ephemeral worktree；必须使用非 force `git worktree remove <path>`，不得删除 branch。dirty、`owner=user` 或 `protected=true` 时只报告或跳过。

`/scan-agent-worktrees` 是唯一 agent worktree / branch 清理入口。它不是只读扫描；用户调用它即授权通过受控 wrapper 删除 clean agent auto worktree 和 zero-ahead `wt-auto-*` agent branch。dirty、`owner=user`、`protected=true`、非 auto root、缺 marker、unique commits、仍被 checkout、pushed/upstream branch 只报告，不自动删除。

agent branch sweep 是 lower-level legacy/manual helper，不作为常规入口；不得用它替代 `/scan-agent-worktrees` 的受控 wrapper。显式 branch 删除也只允许 `git branch -d`，不得使用 `git branch -D`，不得删除有 upstream、origin 同名远端、仍被 checkout、缺少 registry marker、`owner=user` 或 `protected=true` 的分支。

`/ship-task-pr` 是独立的显式远端写入入口，不复用 `/scan-agent-worktrees`。它只能通过受控 wrapper 创建分支、commit、push 和 PR；不得扩展 scan 命令的只读语义。

`/brainstorm-task` 只用于需求澄清、方案比较和风险列举；默认不改文件、不创建分支、不启动 worktree。

`/tdd-task` 只用于明确开发或修 bug；先定位现有测试体系，没有测试体系时只提出最小测试方案，不为文档、规则、小脚本维护强行 TDD，也不得顺手重构。

`/review-pr-local` 是本地只读 PR 审查入口，调用 `.claude/agents/local-pr-reviewer.md` 审查本地 git diff，替代云端 Codex PR Review。它不得运行 `gh pr review --submit`、`gh pr merge` 或 `git push`，不得提交，不得改代码；可选报告只能写入 ignored `.tmp/pr-review/<branch>.md`。

`stop-guard-lite` 在用户明确批准写 Stop hook 前，仍只是模块卡候选。

## 前端边界

Playwright 和 `frontend-polish-lite` 是 claudecode-only、coding-only tools。

它们不进入 global core，不进入 `kb`，不写 global hooks，也不用于所有任务。

只在前端 UI interaction、page behavior、visual、responsive 或 accessibility checks 中使用。

## ECC 与 Superpowers 边界

ECC 已剔除；不得作为默认能力或候选模块恢复。未来重新引入必须重新走模块准入卡。

Superpowers 不是默认 SessionStart，不全量启用，不做 user/global 默认安装。未来试装只能是 local scope / 单仓试验，且必须重新走准入和确认。

## kb 边界

`kb` 是知识库工作流，不能继承 claudecode 开发流程。

不得把 TDD、worktree、PR、subagent-driven development、agent-first、commit-first、Playwright、frontend-polish-lite 或 claudecode self-improvement flow 推入 `kb`。
