# Git Worktree 策略

本文件是 `claudecode` worktree 口径的唯一详细说明。入口文件只保留短规则或指向。

## 分类

- `directed`：用户明确要求创建隔离工作树；branch `wt-directed-<purpose>`；path `C:\Users\apple\_worktrees\claudecode\directed\<purpose>`。
- `auto`：agent 临时隔离工作树；branch `wt-auto-cc-<yyyymmdd-hhmm>-<purpose>-<id>`；path `C:\Users\apple\_worktrees\claudecode\auto\<branch>`。
- repo 外 registry：`C:\Users\apple\_worktrees\claudecode\.registry\<session_id>-<name>.json`，记录 `owner`、`protected`、`session_id`、`agent_id`、`agent_type`、`name`、`purpose`、`path`、`branch`、`created_at`、`cleanup_policy`。
- `legacy`：`worktree-agent-*`、nested `.claude/worktrees/**`、random adjective names、bare `agent-*`；不允许新增。
- 已存在的 locked legacy worktree 是历史残留；本仓不会自动清理，清理必须另起人工确认任务。
- `keep`：`worktree-run-scraping-refactor-phase1`；不自动清理。

## 目的

- 保护本地当前进度，避免被动切分支。
- 避免 agent 直接污染当前工作区。
- 为较大任务或 agent isolation 提供短命隔离执行区。
- 普通 Agent / Explore / review / subagent / `isolation: "worktree"` 自动创建的 worktree 一律视为 `owner=agent` ephemeral。
- 只有 helper `-Owner directed` 或 WorktreeCreate name 显式带 `directed-` / `user-` 的 worktree 才视为 `owner=user` persistent；不得仅凭自然语言推断为 persistent。

## Hook 边界

- active worktree contract 以 `.claude/hooks/worktree-governor-create.ps1`、`.claude/hooks/worktree-governor-remove.ps1` 和 repo 外 registry 为准。
- `WorktreeCreate` 负责 owner 判定、受管 branch/path 生成、repo 外 registry marker 写入。
- `WorktreeRemove` 只允许对 registry 中 `owner=agent`、`protected=false`、位于 auto root 且 clean 的 worktree 执行非 force `git worktree remove <path>`。
- `WorktreeRemove` 不删除 branch；branch 清理必须另起显式手动命令。
- dirty ephemeral worktree 不得强删，只报告 blocker。
- `owner=user` 或 `protected=true` 的 persistent worktree 必须跳过自动清理。
- `WorktreeRemove` 不是阻断型安全边界；安全边界依赖 marker、受控路径、非 force remove，以及 `PreToolUse` 对 raw `git worktree remove --force` 的拦截。
- `SessionEnd` 不清理 worktree。
- `scripts/git/ccw-*` 当前是 legacy/manual tooling，不进入自动 hook contract；后续如需使用，另起任务统一到同一 root/branch/marker 口径。

## Branch 清理

- agent branch sweep 是独立手动工具：`.claude/tools/worktree-governor/sweep_agent_branches.ps1`。
- 它不接入 `WorktreeRemove` hook，不改变 worktree cleanup 基线。
- 只处理 repo 外 registry 中 `owner=agent`、`protected=false`、branch 匹配 `wt-auto-*` 的记录。
- `owner=user`、directed、`protected=true`、仍被任何 git worktree checkout、有 upstream config、或存在 `origin/<branch>` 的分支一律跳过。
- 没有 registry marker 的 `wt-auto-*` branch 只能在 dry-run 结果中列出，不会被处理。
- 当 `git rev-list --count main..<branch>` 为 `0` 时，显式 `-Apply` 模式才允许执行 `git branch -d <branch>`。
- 当独有提交数大于 `0` 时，不删除，标记 `needs-review`，并输出 `git log --oneline main..<branch>`。
- 禁止 `git branch -D`；branch 强删不属于本工具能力。
- archive 模式本轮只支持 `-ArchivePlan` dry-run，展示 archive tag、bundle、patch 的计划路径；不会创建 tag、bundle 或 patch。真正 archive/delete 必须另起显式任务并先 verify。

## VS / Codex 隔离

- VS 只打开主 worktree：`C:\Users\apple\claudecode`。
- Agent auto worktree 只放 `C:\Users\apple\_worktrees\claudecode\auto\**`。
- 不把 auto root 加入 VS workspace。
- 分支清理只根据 repo 外 registry 和 `git worktree list --porcelain` 判定，不根据 VS workspace 状态推断。

## 手动工具

```powershell
.\.claude\tools\worktree-governor\new_worktree.ps1 -Owner directed -Purpose scraping-refactor-phase1 -DryRun
.\.claude\tools\worktree-governor\new_worktree.ps1 -Owner auto -Purpose review-diff -DryRun
.\.claude\tools\worktree-governor\sweep_agent_branches.ps1 -Json
```

不使用 `-DryRun` 创建 worktree 前，必须有明确人工指令。
不使用 `-Apply` 时，branch sweep 只报告决策，不删除 branch。
