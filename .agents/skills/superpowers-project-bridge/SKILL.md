---
name: superpowers-project-bridge
description: 仓库级 Superpowers 方法论桥接；只路由方法触发条件，不覆盖本仓 AGENTS、Git、安全、发布或验收边界。
---

# superpowers-project-bridge

## trigger

- 用户明确提到 Superpowers。
- 任务需要 brainstorming、using-git-worktrees、writing-plans、TDD、debugging、executing-plans 或 requesting-code-review 的方法提示。

## scope

- 将 Superpowers 视为方法论 Router。
- 仍以 `AGENTS.md`、`work_area_registry.md` 和 `docs/workflows/` 为本仓规则来源。
- 具体触发条件以 `AGENTS.md` 的 Methodology Router 为准。
- `using-git-worktrees` 的策略：
  默认在主仓工作。开 worktree 是显性动作，仅在以下两种触发下进行：
  1. 用户消息中包含明示词："开树"、"在工作树里做"、"detached worktree"、"使用 worktree" 之一；
  2. 上游 plan 文件（`IMPLEMENTATION_PLAN.md` / `TASK_BRIEF.md` / `CODEX_PROMPT.md`）明确标注 "requires_worktree: true" 或等价中文 "需要 worktree"。
- 不构成开树触发：任务涉及多个文件、任务跨多个阶段、任务被 `writing-plans` / `executing-plans` 路由命中、任务被判定为 non-trivial / 高风险路径。
- `scripts/workflow/worktree-start.ps1` 仍可被显性调用；其行为不变。

## forbidden actions

- 不覆盖本仓规则。
- 不改变 Git、安全、发布或验证边界。
- 不启用 hook、自动 PR、记忆晋升或任务恢复。
- 不触碰凭据、token、cookie、auth 或 proxy secret。

## verification expectation

- 若使用 Superpowers 辅助任务，仍需执行本仓验证。
- 收尾说明使用了哪些本仓脚本或验证证据。
