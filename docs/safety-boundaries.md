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
- `git worktree remove`

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

`stop-guard-lite` 在用户明确批准写 Stop hook 前，仍只是模块卡候选。

## 前端边界

Playwright 和 `frontend-polish-lite` 是 claudecode-only、coding-only tools。

它们不进入 global core，不进入 `kb`，不写 global hooks，也不用于所有任务。

只在前端 UI interaction、page behavior、visual、responsive 或 accessibility checks 中使用。

## ECC 与 Superpowers 边界

ECC 不是本仓当前工作流来源。可以盘点本地 ECC residue，但删除或归档需要单独批准的 cleanup plan。

Superpowers 不是默认 SessionStart。Superpowers/TDD 只能在准入和确认后作为 task-scoped 路线。

## kb 边界

`kb` 是知识库工作流，不能继承 claudecode 开发流程。

不得把 TDD、worktree、PR、subagent-driven development、agent-first、commit-first、Playwright、frontend-polish-lite、ECC cleanup 或 claudecode self-improvement flow 推入 `kb`。
