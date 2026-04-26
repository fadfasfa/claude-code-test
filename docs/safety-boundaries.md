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

读取 Markdown / text / code 文件时，不传 PDF `pages` 参数，不传空 `pages` 参数，也不混用 PDF 专用参数。

避免一次性读取超大范围。优先使用目录枚举、`rg`、`Select-String`、`Get-Content -TotalCount` 或小范围分段读取来确认候选位置和少量上下文。

若 Read 失败且原因是空 `pages`、`pages` 参数不适用于 Markdown/text/code、行号范围、空参数或工具参数失败，失败一次后禁止用相同参数重试。Markdown/text/code 的主线程只读 fallback 是 `Get-Content -LiteralPath <path> -Encoding UTF8 -Raw`；该 fallback 只能读取文件，不得写文件。

对大型工作区先搜索关键词和目录结构，不全量读文件。已知 `target_work_area` 时，先检索 `work_area_registry.md` 中该工作区条目，再列工作区一级目录；不要把 `Glob <work_area>/**/*` 作为第一步。只有明确需要更多候选文件时，才扩大 Glob 或跨子区搜索。

严格只读验收或 smoke test 中，不主动执行预期会失败的 Bash，也不通过危险失败试探来证明 hook 生效。

## Windows tooling 边界

- Windows native Python / PowerShell 不应直接使用 `/c/Users/...` Bash 风格路径。
- 进入 native Python、PowerShell 或文件工具参数时，使用 `C:/Users/...` 或 Windows 可解析路径。
- 中文、替换字符、大段 Markdown 输出在 Windows 控制台可能触发编码问题。
- 优先用 Read、Grep、`rg`、`Select-String` 分段读取；必须用 Python 输出时显式控制 UTF-8 或限制输出范围。
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

agent branch sweep 不接入 hook，不改变 worktree cleanup 基线；只能基于 repo 外 registry 和 `git worktree list --porcelain` 判定。显式 `-Apply` 也只允许 `git branch -d`，不得使用 `git branch -D`，不得删除有 upstream、origin 同名远端、仍被 checkout、缺少 registry marker、`owner=user` 或 `protected=true` 的分支。

`stop-guard-lite` 在用户明确批准写 Stop hook 前，仍只是模块卡候选。

## 前端边界

Playwright 和 `frontend-polish-lite` 是 claudecode-only、coding-only tools。

它们不进入 global core，不进入 `kb`，不写 global hooks，也不用于所有任务。

只在前端 UI interaction、page behavior、visual、responsive 或 accessibility checks 中使用。

## ECC 与 Superpowers 边界

ECC 已剔除；不得作为默认能力或候选模块恢复。未来重新引入必须重新走模块准入卡。

Superpowers 不是默认 SessionStart。Superpowers/TDD 只能在准入和确认后作为 task-scoped 路线。

## kb 边界

`kb` 是知识库工作流，不能继承 claudecode 开发流程。

不得把 TDD、worktree、PR、subagent-driven development、agent-first、commit-first、Playwright、frontend-polish-lite 或 claudecode self-improvement flow 推入 `kb`。
