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

## forbidden actions

- 不覆盖本仓规则。
- 不改变 Git、安全、发布或验证边界。
- 不启用 hook、自动 PR、记忆晋升或任务恢复。
- 不触碰凭据、token、cookie、auth 或 proxy secret。

## verification expectation

- 若使用 Superpowers 辅助任务，仍需执行本仓验证。
- 收尾说明使用了哪些本仓脚本或验证证据。
