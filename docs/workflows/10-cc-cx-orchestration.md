# CC / CX Orchestration

本文件只保留当前 CC -> CX 契约摘要。详细阶段、模板和故障恢复见 `cc-cx-delegation.md`。

## Roles

- CC / Claude Code：需求澄清、任务派发、计划审批、结果审查。
- Codex Researcher：只读探查 protected path，整理证据、定位文件、梳理调用链。
- Codex Planner：把探查结果收敛为可审批计划，明确范围、风险、验证入口和停止条件。
- Codex Executor：在 plan approved 后修改文件、生成 diff、执行最小验证；若连续 3 次 shell failure，必须停止并报告已完成/未完成步骤，不得继续猜测或换壳重试。
- Codex Reviewer：复核 changed files、diff summary、validation result，并对失败或回滚状态给出结论。

## Current Contract

- `.claude/settings.json` 已启用 OpenAI 官方 Codex plugin，作为 CC 调用 CX 的默认主路。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。
- `cx-exec.ps1` 如出现只作为 legacy/compat，不再作为 CC-CX 主流程要求。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录，不再作为默认验收接口。

## Direct Access Rule

- NORMAL 状态下，CC 直接写入仅限 `.claude/plans/**` 和 `.state/cc-work/**`。
- CC 可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 以完成控制面审查。
- NORMAL 状态下，CC 不得直接 `Read` / `Glob` / `Grep` / `LS` 业务 protected path。
- CC 不得直接 `Edit` / `Write` / `MultiEdit` protected path。
- CC 不得通过 Bash 直接探查、执行、删除、移动、写入或批量格式化 protected path。
- CC 可直接使用 `git status` / `git diff` / `git log` 做只读审查。

## Delegation Guard

Claude Code 通过 `.claude/settings.json` 注册 `PreToolUse` Delegation Guard。Guard v4 的默认决策如下：

- allow：`.claude/plans/**`、`.state/cc-work/**`、控制面文档只读、以及显式 allowlist 的 OpenAI Codex plugin control-plane 命令。
- deny：NORMAL/CX_DEGRADED 下 CC 对业务 protected path 的直接探查、修改或 Bash 执行。
- break-glass：`.state/cc-work/cc-cx-state.json` 可声明 `CC_BG_READ` 或 `CC_BG_WRITE`；read 授权本会话持续有效，write 必须每个 approved plan 单独列出 `approved_files`。
- degraded：`CX_DEGRADED` 下不得继续启动或恢复 Codex 执行线程，只允许 status/cancel/report 类收敛动作。
- git：未授权 `git add` / `git commit` / `git push` 直接 deny；push 不继承 commit 授权。

Guard 文件和 `.claude/settings.json` 只能通过独立治理任务修改，不得混入业务代码修复。
