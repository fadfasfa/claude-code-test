# CC / CX Orchestration

本文件只保留当前 CC -> CX 契约摘要。详细阶段、模板和故障恢复见 `cc-cx-delegation.md`。

## Roles

- CC / Claude Code：需求澄清、任务派发、计划审批、结果审查。
- Codex Researcher：只读探查 protected path，整理证据、定位文件、梳理调用链。
- Codex Planner：把探查结果收敛为可审批计划，明确范围、风险、验证入口和停止条件。
- Codex Executor：在 plan approved 后修改文件、生成 diff、执行最小验证。
- Codex Reviewer：复核 changed files、diff summary、validation result，并对失败或回滚状态给出结论。

## Current Contract

- `.claude/settings.json` 已启用 OpenAI 官方 Codex plugin，作为 CC 调用 CX 的默认主路。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。
- 不再维护 `cx-exec` 作为 fallback 或 legacy 主路。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录，不再作为默认验收接口。

## Direct Access Rule

- CC 直接可读可写路径仅限 `.claude/plans/**` 和 `.state/cc-work/**`。
- CC 不得直接 `Read` / `Glob` / `Grep` / `LS` protected path。
- CC 不得直接 `Edit` / `Write` / `MultiEdit` protected path。
- CC 不得通过 Bash 直接探查、执行或修改 protected path。
- CC 可直接使用 `git status` / `git diff` / `git log` 做只读审查。

## Delegation Guard

Claude Code 通过 `.claude/settings.json` 注册 `PreToolUse` Delegation Guard。Guard v3 的默认决策如下：

- deny：CC 对 protected path 的直接探查、修改或 Bash 执行。
- allow：`.claude/plans/**`、`.state/cc-work/**`、以及显式 allowlist 的 Codex control-plane 命令。
- ask：高风险 Git / destructive Bash，例如 `git reset --hard`、`git clean -fd`、`git checkout -- <path>`、`git commit`、`git push`。

Guard 文件和 `.claude/settings.json` 只能通过独立治理任务修改，不得混入业务代码修复。
