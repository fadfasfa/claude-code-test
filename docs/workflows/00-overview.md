# Workflow Overview

`docs/workflows/` 只保留当前有效、短、可执行的仓库规则。历史报告、长解释、probe 和一次性验收不放在 active 层。

## Default Flow

1. 先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
2. 入口选择：
   - Claude Code 入口读 `CLAUDE.md`、`PROJECT.md`、`docs/index.md`。
   - Codex 入口读 `AGENTS.md`、`PROJECT.md`、`docs/index.md`。
3. 写入前从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. `S/M/L` 风险路由、官方 Superpowers 方法论、worktree、计划、验证、review 和收尾流程以 `.agents/skills/superpowers-project-bridge/SKILL.md` 为准。
5. 当前轮已经明确授权或计划已批准的动作，agent 按授权范围执行并验证，不重复要求业务层确认。
6. 修改后运行最小有效验证；无法验证时说明原因。
7. 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才只暂存本轮修改文件并 commit。
8. 禁止 `git add .`；push、PR、merge 或 discard 未获明确授权时禁止主动执行；用户明确要求后由 agent 自行完成并验证结果，不要求用户手动输入命令。高危操作按 `07-high-risk-safety.md` 和 bridge 执行。

## Canonical Docs

- `independent-agent-workflow.md`
- `codex-execution-boundary.md`
- `.agents/skills/superpowers-project-bridge/SKILL.md`
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

当前基线保持轻量：普通仓库编辑按当前轮任务直接执行；`M/L` 或高风险类别按 bridge 执行；commit、push、PR、merge 和 discard 只在当前轮明确授权范围内执行。
