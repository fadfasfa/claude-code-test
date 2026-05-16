# Claude Code Entry

本文件是 `claudecode` 仓库的 Claude Code 项目级入口。Codex 规则以 `AGENTS.md` 为准。

## Role

- CC：planning、supervision、review。
- CX：通过 `cx-exec.ps1` 执行读写和验证。
- CC 不直接扩展本仓 Codex skill、hook、memory 或 workflow 能力。

## CX Boundary

- 调用入口：根目录 `cx-exec.ps1`。
- executor：`scripts/workflow/cx-exec.ps1`。
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`。
- 结构化结果：`.state/workflow/tasks/<task_id>/`。
- 普通 CX 修改不要求生成计划、Markdown report、probe 或 archive 证据文件。

CC 如需让 CX 使用 worktree，必须在上游任务中显式写明 `requires_worktree: true` 并等用户确认。

