# Claude Code Entry

本文件是 `claudecode` 仓库的 Claude Code 入口。Codex 规则以 `AGENTS.md` 为准。

## Role

- CC：planning、supervision、review。
- CX：通过 `cx-exec.ps1` 执行读写和验证。
- Codex-led standalone mode：用户直接调用 Codex 时，Codex 可按 `AGENTS.md`、`docs/index.md` 和用户任务独立完成普通代码任务。
- CC-led supervised mode：CC 涉及实现性修改时通过 `.\cx-exec.ps1` 委派 CX；`.\cx-exec.ps1` 不是 Codex 的唯一入口。

## CX Boundary

- 默认使用简体中文。
- 调用入口：根目录 `cx-exec.ps1`。
- executor：`scripts/workflow/cx-exec.ps1`。
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`。
- 结构化结果：`.state/workflow/tasks/<task_id>/`。
- 普通任务不生成计划、Markdown report、probe 或 archive 证据文件。

## Delegation Guard

- `.claude/settings.json` 注册 Claude Code `PreToolUse` Delegation Guard。
- Guard 只约束 CC 的 `Edit`、`Write`、`MultiEdit` 和修改型 `Bash`；不影响用户直接调用 Codex。
- Guard 允许 CC 执行 `.\cx-exec.ps1` 或 `pwsh -File .\cx-exec.ps1` 作为委派入口；直接修改该入口文件仍被阻止。
- Guard 生效后，CC 默认不得直接修改受保护业务工作区、workflow 控制面、仓库入口文件、`.claude/settings.json`、`.claude/hooks/**` 或 `.agents/skills/**`。
- 只有用户显性授权“允许 CC 直接修改”时，CC 才能绕过默认委派要求。

CC 如需让 CX 使用 worktree，必须在上游任务中显式写明 `requires_worktree: true` 并等待用户确认。
