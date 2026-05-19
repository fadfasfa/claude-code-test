# Workflow Overview

`docs/workflows/` 只保留当前有效、短、可执行的仓库规则。历史报告、长解释、probe 和一次性验收不放在 active 层。

## Default Flow

1. 先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
2. 入口选择：
   - Claude Code 入口读 `CLAUDE.md`、`PROJECT.md`、`docs/index.md`。
   - Codex 入口读 `AGENTS.md`、`PROJECT.md`、`docs/index.md`。
3. 写入前从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. 默认在主仓小步执行；只有用户明确要求或上游任务标注 `requires_worktree: true` 时才进入 worktree 流程。
5. 修改后运行最小有效验证；无法验证时说明原因。
6. 验证通过后，按本轮授权只暂存本轮修改文件并 commit。
7. 禁止 `git add .`；禁止默认 push；高危操作按 `07-high-risk-safety.md` 确认。

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
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；当前工作流不依赖 `.state/workflow/tasks/result.json`。
- 本机计划、协作、交接和审查草稿可写入 `.state/cc-work/**`；普通任务不强制生成这些文件。
- Claude Code 原生本机计划草稿可写入 `.claude/plans/**`，不需要额外授权或特殊条件，默认不提交。
- 不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
