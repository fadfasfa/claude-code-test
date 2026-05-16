# claudecode Codex Rules

`claudecode` 是个人总编程仓、多子项目母仓和 Codex 编程主执行仓。Codex 是当前唯一主流程；`CLAUDE.md` 与 `.claude/README.md` 只保留 Claude Code 边界，不作为 Codex 规则来源。

## Default

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 仓库根目录是治理、路由和工具骨架，不是默认业务写入面。
- 业务修改必须先落到明确子项目或已登记工作区；写入范围以 `docs/workflows/work_area_registry.md` 为准。
- Windows 默认使用 PowerShell。
- 默认在当前工作树小步执行；不自动创建 worktree、分支、计划文件、Markdown report、probe 或 archive 证据文件。
- 修改后运行最小有效验证；无法验证时说明具体原因。

## Canonical Docs

- 文档入口：`docs/index.md`
- workflow 总览：`docs/workflows/00-overview.md`
- 执行边界：`docs/workflows/codex-execution-boundary.md`
- 工作区边界：`docs/workflows/work_area_registry.md`
- 目录职责：`docs/workflows/repository-layout.md`
- worktree 策略：`docs/workflows/worktree-policy.md`
- skill inventory：`docs/workflows/agent-skill-inventory.md`

## Git And Safety

- 不默认执行 `git add`、`git commit`、`git push`、`git clean`、`git reset`、`git rebase`、`git stash`。
- commit / push / PR / merge / amend 必须得到用户明确授权。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、私有配置、`.env`、`auth.json`、`local.yaml`、`proxies.json`。
- `run/**` 中的 raw data、原始抓取结果、不可重建资产和当前脏树默认受保护。
- 任何备份失败都必须立即停止，不继续删除、覆盖、移动或其他破坏性动作。

## Skills

- 非琐碎代码、脚本、配置或 workflow 实现任务必须触发 `karpathy-project-bridge`。
- 前端 UI / 视觉 / 交互任务还必须触发 `frontend-design-project-bridge`。
- 完成前默认使用 `repo-verification-before-completion` 口径报告证据。
- `.agents/skills/README.md` 是仓库级 Codex skill 白名单入口。
- 不恢复 command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。

## Completion Report

非琐碎任务收尾说明：修改文件、是否触碰 `run/**`、是否执行删除/清理/移动、是否 staging/commit/push、diff 摘要、验证命令与结果，以及 acceptance gate。
