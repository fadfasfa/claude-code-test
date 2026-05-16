# Workflow Overview

`docs/workflows/` 只保存当前有效、短、可执行的仓库工作流规则。历史报告、长解释和一次性探针不放在 active 层。

## 默认流程

1. 先读 `AGENTS.md`、`PROJECT.md`、`docs/index.md`。
2. 写入前从 `docs/workflows/work_area_registry.md` 选择目标工作区。
3. 默认在主仓执行；worktree 只有用户显性触发或上游任务明确 `requires_worktree: true` 时才创建。
4. 小步修改，避免顺手重构。
5. 运行最小有效验证；无法验证时说明具体原因。
6. commit / push / PR / merge 只在用户明确授权下执行。

## Canonical Rules

- 执行边界：`docs/workflows/codex-execution-boundary.md`
- CC/CX 契约：`docs/workflows/10-cc-cx-orchestration.md`
- 目录职责：`docs/workflows/repository-layout.md`
- 工作区边界：`docs/workflows/work_area_registry.md`
- worktree 策略：`docs/workflows/worktree-policy.md`
- skill inventory：`docs/workflows/agent-skill-inventory.md`

## Artifact Boundary

- 普通 Codex 修改任务只产出目标 diff 和对话摘要。
- `cx-exec.ps1` 的机器结果固定写入 `.state/workflow/tasks/<task_id>/`。
- `docs/plans/`、Markdown report、probe 文件和 archive 证据文件不是普通任务默认产物。
- 历史资料只放 `docs/reference/` 或 `docs/archive/`，默认不读。
