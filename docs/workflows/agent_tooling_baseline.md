# Agent Tooling Baseline

本文件只保留仓库内工具基线短清单。全局配置、账号、proxy 和额度不在本仓维护。

## Source Of Truth

- 入口：`AGENTS.md`、`PROJECT.md`、`README.md`、`docs/index.md`
- workflow：`docs/workflows/`
- 工作区：`docs/workflows/work_area_registry.md`
- skills：`.agents/skills/README.md` 和 `docs/workflows/agent-skill-inventory.md`
- 脚本：`scripts/`；旧 `scripts/workflow/` 主流程已移除

## Defaults

- Claude Code 和 Codex 都可以独立完成普通仓库任务。
- OpenAI Codex plugin 可以保留启用状态；Claude Code 没有用户当前轮显性点名或命令时不得调用、委派、审查或触发 Codex / CX。
- plugin 启用不等于 review gate 启用，review gate 默认禁用。
- 旧 CC-CX 执行脚本如出现只作为 legacy/compat 线索，不作为主流程、fallback 主路或验收接口。
- Windows 默认 shell 是 PowerShell。
- 普通仓库任务不修改全局 Claude Code、Codex、Superpowers、CLI、VS plugin、Codex App 或 proxy 配置。
- 不设置项目级 `.codex/config.toml`、repo-local Codex hook 或 repo-local MCP 配置。
- 不新增复杂编排器；`.claude/settings.json` 只保留 Claude Code 项目权限和 plugin 可用状态。
- `full-access` profile 只能人工选择，不是仓库默认权限。

## Skill Boundary

- 仓库级 skill 只保留 inventory 中登记项。
- 不恢复 memory、learning promotion、自动 PR shipping、高权限 worktree governance 或 task resume skill。

## Verification

完成前说明修改文件、验证命令、是否触碰受保护路径，以及是否执行删除/清理/移动或 staging/commit/push。
