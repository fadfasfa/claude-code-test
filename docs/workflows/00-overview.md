# Workflow Overview

`docs/workflows/` 只保留当前有效、短、可执行的仓库规则。历史报告、长解释、probe 和一次性验收不放在 active 层。

## Default Flow

1. 先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
2. 只读探查可以直接执行；凡涉及非只读探查、非平凡文件修改、workflow/config/skill/hook 修改、git 写操作、worktree 操作或破坏性命令，必须先输出计划并等待用户确认。
3. 普通极小单文件修改若不涉及 workflow/config/skill/hook、git 写、worktree 或破坏性操作，且用户当前轮明确要求直接执行，可以跳过计划确认；仍需按授权范围小步修改并验证。
4. 计划必须包含：`git status`、预计修改文件、修改内容、不修改范围、验证命令、Git 处理方式；确认后按计划小步执行，范围变化时重新确认。
5. 入口选择：
   - Claude Code 入口读 `CLAUDE.md`、`PROJECT.md`、`docs/index.md`。
   - Codex 入口读 `AGENTS.md`、`PROJECT.md`、`docs/index.md`。
6. 写入前从 `docs/workflows/work_area_registry.md` 选择目标工作区。
7. 默认在主仓小步执行；只有用户明确要求或上游任务标注 `requires_worktree: true` 时才进入 worktree 流程。
8. 修改后运行最小有效验证；无法验证时说明原因。
9. 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才只暂存本轮修改文件并 commit。
10. 禁止 `git add .`；禁止默认 push；高危操作按 `07-high-risk-safety.md` 确认。

## Canonical Docs

- `independent-agent-workflow.md`
- `codex-execution-boundary.md`
- `07-high-risk-safety.md`
- `repository-layout.md`
- `work_area_registry.md`
- `worktree-policy.md`
- `ultraplan-adoption-note.md`
- `agent-skill-inventory.md`

## Artifact Boundary

- 普通任务只产出目标 diff 和对话摘要。
- `.state/workflow/**` 是旧 CC-CX 工作流遗留运行态目录；当前工作流不依赖旧任务结果文件。
- 本机计划、协作、交接和审查草稿可写入 `.state/cc-work/**`；普通任务不强制生成这些文件。
- Claude Code 原生本机计划草稿可写入 `.claude/plans/**`，不需要额外授权或特殊条件，默认不提交。
- 不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。

当前基线保持轻量：非平凡修改先计划，经确认后执行，验证后报告，commit 需单独授权。
