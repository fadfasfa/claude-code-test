# Claude Code Entry

本文件只保留 Claude Code 入口边界。当前 CC/CX 主契约见 `docs/workflows/cc-cx-delegation.md`。

## Role

- CC / Claude Code：需求澄清、任务派发、计划审批、结果审查。
- CX / Codex：探查、计划证据收集、代码定位、patch 生成、apply、最小验证。
- 用户直接调用 Codex 时，Codex 仍保留 standalone 执行能力。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin。

## Hard Boundary

- 默认使用简体中文。
- NORMAL 状态下，CC 只允许直接写 `.claude/plans/**` 和 `.state/cc-work/**`；可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 这类控制面文档。
- NORMAL 状态下，CC 不得直接 `Read` / `Glob` / `Grep` / `LS` 业务 protected path。
- NORMAL 状态下，CC 不得直接 `Edit` / `Write` / `MultiEdit` protected path。
- CC 不得通过 Bash 直接探查、执行、删除、移动、写入或批量格式化 protected path。
- 允许的 Codex control-plane 命令由 `.claude/hooks/cc-delegation-guard.ps1` 显式 allowlist 管理。
- Guard 文件和 `.claude/settings.json` 只能在独立治理任务中由 Codex 修改，不得混入业务修复。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。

## Guard State

- Guard 状态源优先为 `.state/cc-work/cc-cx-state.json`；缺失时按 `NORMAL` 处理。
- `NORMAL`：CX-first，CC 不直接访问业务 protected path。
- `CX_DEGRADED`：官方 Codex Plugin、线程、本地命令或只读 smoke 失败后使用；不得继续启动或恢复 Codex 执行线程，只允许 status/cancel/report 类收敛动作。
- `CC_BG_READ`：用户授权后本会话持续有效；CC 可直接 `Read` / `Grep` / `Glob` / `LS` protected path，但不得修改或通过 Bash 访问。
- `CC_BG_WRITE`：必须每个 approved plan 单独授权；CC 只能 `Edit` / `Write` / `MultiEdit` 状态文件 `approved_files` 中列出的文件。

## Review Surface

- CC 可审查 Codex 最终报告、`git diff` 摘要、计划文件和 `.state/cc-work/**` 草稿。
- `git add` 默认拒绝；commit 必须明确授权；push 永远必须单独授权，不能继承 commit 授权。
