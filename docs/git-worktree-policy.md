# Git Worktree Policy

本文件是 `claudecode` worktree 口径唯一详细说明。入口文件只保留短规则或指向。

## 分类

- `directed`：用户明确要求创建隔离工作树；branch `wt-directed-<purpose>`；path `C:\Users\apple\_worktrees\claudecode\directed\<purpose>`。
- `auto`：agent 临时隔离工作树；branch `wt-auto-cc-<yyyymmdd-hhmm>-<purpose>-<id>`；path `C:\Users\apple\_worktrees\claudecode\auto\<branch>`。
- `legacy`：`worktree-agent-*`、nested `.claude/worktrees/**`、random adjective names、bare `agent-*`；不允许新增。
- 已存在的 locked legacy worktree 是历史残留；本仓不会自动清理，清理必须另起人工确认任务。
- `keep`：`worktree-run-scraping-refactor-phase1`；不自动清理。

## 目的

- 保护本地当前进度，避免被动切分支。
- 避免 agent 直接污染当前工作区。
- 为较大任务提供隔离执行区。
- 任务完成、审查、合并、本地同步后，才进入人工确认的无用树清理。

## Hook 边界

- 自动 hook 只保留 `WorktreeCreate` 命名校验和 `PreToolUse` 裸 `git worktree` / 危险 shell 命令拦截。
- `WorktreeRemove`、`SessionEnd` 不自动清理 worktree。
- 删除 / 清理 worktree 不属于默认自动机制；必须人工确认或作为专门 final cleanup step 手动处理。

## 手动工具

```powershell
.\.claude\tools\worktree-governor\new_worktree.ps1 -Owner directed -Purpose scraping-refactor-phase1 -DryRun
.\.claude\tools\worktree-governor\new_worktree.ps1 -Owner auto -Purpose review-diff -DryRun
```

不使用 `-DryRun` 创建 worktree 前，必须有明确人工指令。
