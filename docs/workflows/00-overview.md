# Workflow Overview

`docs/workflows/` 只保留当前有效、短、可执行的仓库规则。历史报告、长解释、probe 和一次性验收不放在 active 层。

## Default Flow

1. 先读 `AGENTS.md`、`PROJECT.md`、`docs/index.md`。
2. 写入前从 `docs/workflows/work_area_registry.md` 选择目标工作区。
3. 默认在主仓小步执行；只有用户明确要求或上游任务标注 `requires_worktree: true` 时才进入 worktree 流程。
4. 修改后运行最小有效验证；无法验证时说明原因。
5. Git 写入和发布动作只在用户明确授权下执行。
6. 在 CC 监督模式下，CC 只做 intake / approval / review；受保护路径的探查、patch 和验证交给 Codex。

## Canonical Docs

- `codex-execution-boundary.md`
- `codex-runtime-patch.md`
- `10-cc-cx-orchestration.md`
- `cc-cx-delegation.md`
- `repository-layout.md`
- `work_area_registry.md`
- `worktree-policy.md`
- `agent-skill-inventory.md`

## Artifact Boundary

- 普通任务只产出目标 diff 和对话摘要。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。
- CC 计划、协作、交接和审查草稿可写入 `.state/cc-work/**`；普通任务不强制生成这些文件。
- Claude Code 原生本机计划草稿可写入 `.claude/plans/**`，不需要额外授权或特殊条件，默认不提交。
- CC 直连工具默认不得探查或修改 protected path；这类动作应通过 Codex control-plane 委派。
- 不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
