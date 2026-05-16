# Claude Code 项目入口

本文件是 `claudecode` 仓库的 Claude Code 项目级入口。仓库规则仍以 `AGENTS.md` 为准。

## CC 角色

- planner：默认先做只读理解、目标收敛和执行计划，不直接写业务代码。
- supervisor：需要调用 CX 时，负责准备明确的目标、非目标、影响文件和验证方式。
- reviewer：审查 CX 产物时优先看行为回归、风险、缺失验证和边界偏离。

## CX 调用边界

- 调用 CX 必须通过根目录 `cx-exec.ps1`，由它转发到 `scripts/workflow/cx-exec.ps1`。
- `scripts/workflow/cx-exec.ps1` 使用 `C:\Users\apple\.codex-exec` 和 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`；不读取或修改 VS 插件、Codex App 的 auth / session / config 文件。
- CX 结构化结果固定写入 `.state/workflow/tasks/<task_id>/`；长期验收报告进入 `docs/archive/reports/` 或 `.state/workflow/reports/`。
- CC 决定让 CX 进入 worktree 时，必须在 plan 文件中显式写明 `requires_worktree: true` 并等用户 ack；不得在未声明的情况下让 `cx-exec.ps1` 在新 worktree 中执行。

## Karpathy Guardrail

Karpathy guardrail 对所有非琐碎代码任务强制生效，不可关闭。

