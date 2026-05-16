# Agent Tooling Baseline

本文件是仓库内工具基线短清单，不是全局工具事实源。全局配置、账号、proxy 和额度不在本仓维护。

## Current Sources

- 仓库入口：`AGENTS.md`、`PROJECT.md`、`README.md`、`docs/index.md`
- 工作区注册表：`docs/workflows/work_area_registry.md`
- Codex skill 白名单：`.agents/skills/README.md`
- workflow 规则：`docs/workflows/`
- workflow 脚本：`scripts/workflow/`

## Current Defaults

- Codex 是当前唯一主流程；Claude Code 只保留白板和必要接口。
- Windows 默认 shell 是 PowerShell。
- 默认权限口径是低摩擦 granular；高危动作由用户授权和规则闸门控制。
- 普通仓库任务不修改全局 Claude Code、Codex、Superpowers、CLI、VS plugin、Codex App 或 proxy 配置。
- 不设置项目级 `.codex/config.toml`、repo-local Codex hook 或 repo-local MCP 配置。
- `full-access` profile 只能人工选择，不是仓库默认权限。

## Skill Baseline

仓库级 Codex skill 只以 `.agents/skills/README.md` 和 `docs/workflows/agent-skill-inventory.md` 为准。

保留方向：

- `karpathy-project-bridge`
- `frontend-design-project-bridge`
- `superpowers-project-bridge`
- `repo-maintenance`
- `repo-module-admission`
- `repo-local-pr-review`
- `repo-verification-before-completion`

不恢复 memory、learning promotion、长期上下文晋升、自动 PR shipping、高权限 worktree governance 或 task resume skill。

## Verification Baseline

完成前报告修改文件、是否触碰受保护路径、是否执行删除/清理/移动、是否 staging/commit/push，以及验证命令和结果。
