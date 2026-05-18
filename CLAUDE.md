# Claude Code Entry

本文件只保留 Claude Code 入口边界。当前 CC/CX 主契约见 `docs/workflows/cc-cx-delegation.md`。

## Role

- CC / Claude Code：需求澄清、任务派发、计划审批、结果审查。
- CX / Codex：探查、计划证据收集、代码定位、patch 生成、apply、最小验证。
- 用户直接调用 Codex 时，Codex 仍保留 standalone 执行能力。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin。

## Hard Boundary

- 默认使用简体中文。
- CC 只允许直接读写 `.claude/plans/**` 和 `.state/cc-work/**`。
- CC 不得直接 `Read` / `Glob` / `Grep` / `LS` protected path。
- CC 不得直接 `Edit` / `Write` / `MultiEdit` protected path。
- CC 不得通过 Bash 直接探查、执行或修改 protected path。
- 允许的 Codex control-plane 命令由 `.claude/hooks/cc-delegation-guard.ps1` 显式 allowlist 管理。
- Guard 文件和 `.claude/settings.json` 只能在独立治理任务中由 Codex 修改，不得混入业务修复。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。

## Review Surface

- CC 可审查 Codex 最终报告、`git diff` 摘要、计划文件和 `.state/cc-work/**` 草稿。
- commit / push / merge / reset / clean 仍需用户明确授权。
