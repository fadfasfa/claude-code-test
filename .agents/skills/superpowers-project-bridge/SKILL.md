---
name: superpowers-project-bridge
description: 极小边界版 Superpowers 桥接；仅作为能力索引，不覆盖本仓 AGENTS、Git、安全或验证边界。
---

# superpowers-project-bridge

## trigger

- 用户明确提到 Superpowers。
- 需要确认可用能力边界，但不需要新增仓库 workflow。

## scope

- 将 Superpowers 视为能力索引。
- 仍以 `AGENTS.md`、`work_area_registry.md` 和 `docs/workflows/` 为本仓规则来源。

## forbidden actions

- 不覆盖本仓规则。
- 不改变 Git、安全、发布或验证边界。
- 不启用 hook、自动 PR、记忆晋升或任务恢复。
- 不触碰凭据、token、cookie、auth 或 proxy secret。

## verification expectation

- 若使用 Superpowers 辅助任务，仍需执行本仓验证。
- 收尾说明使用了哪些本仓脚本或验证证据。
