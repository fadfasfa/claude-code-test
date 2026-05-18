# Agent Tooling Baseline

本文件只保留仓库内工具基线短清单。全局配置、账号、proxy 和额度不在本仓维护。

## Source Of Truth

- 入口：`AGENTS.md`、`PROJECT.md`、`README.md`、`docs/index.md`
- workflow：`docs/workflows/`
- 工作区：`docs/workflows/work_area_registry.md`
- skills：`.agents/skills/README.md` 和 `docs/workflows/agent-skill-inventory.md`
- 脚本：`scripts/`；旧 `scripts/workflow/` 主流程已移除

## Defaults

- Codex-led standalone mode 是当前主流程之一：用户直接调用 Codex 时，Codex 可独立完成普通代码任务。
- CC-led supervised mode 中，Claude Code 负责目标理解、计划收敛、过程监督、diff 审查和结果验收；CX / Codex 负责复杂探查、实现、运行验证和第二意见。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin；plugin 启用不等于 review gate 启用，review gate 默认禁用，也不维护 `cx-exec` fallback。
- Windows 默认 shell 是 PowerShell。
- 普通仓库任务不修改全局 Claude Code、Codex、Superpowers、CLI、VS plugin、Codex App 或 proxy 配置。
- 不设置项目级 `.codex/config.toml`、repo-local Codex hook 或 repo-local MCP 配置。
- 允许唯一的 repo-local Claude Code `PreToolUse` Delegation Guard：`.claude/settings.json` + `.claude/hooks/**`。
- `full-access` profile 只能人工选择，不是仓库默认权限。

## Skill Boundary

- 仓库级 skill 只保留 inventory 中登记项。
- 不恢复 memory、learning promotion、自动 PR shipping、高权限 worktree governance 或 task resume skill。

## Verification

完成前说明修改文件、验证命令、是否触碰受保护路径，以及是否执行删除/清理/移动或 staging/commit/push。
