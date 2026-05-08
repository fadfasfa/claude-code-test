# Agent 工具基线

本文件记录 `claudecode` 仓库内工具基线，不是全局事实源。

Codex 是当前唯一主流程。Claude Code 只保留空白占位。

## 范围

- 仓库：`C:\Users\apple\claudecode`
- 工作区注册表：`work_area_registry.md`
- 仓库级 skill 白名单：`.agents/skills/README.md`
- 全局工具 inventory 在仓库外维护，不在这里重复。

## 边界

- 普通仓库任务不得修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS plugin、Codex App 或代理配置。
- 不修改 `C:\Users\apple\.claude`、`C:\Users\apple\.codex`、全局 hooks、全局 skills、全局 AGENTS / CLAUDE 文件或 KB 仓库，除非用户明确纳入范围。
- 不触碰凭据、auth 文件、token、cookie、API key、proxy secret 或私有配置。

## 当前仓库级 Skill

仓库级 Codex skill 只以 `.agents/skills/README.md` 白名单为准：

- `repo-module-admission`
- `repo-verification-before-completion`

不新增 memory、learning promotion、长期上下文晋升、PR shipping、worktree governance 或 task resume skill。

## 当前工具默认

- Windows 默认 shell 是 PowerShell。
- Git 操作默认只读。
- 不设置项目级 `.codex/config.toml`。
- 不设置 repo-local Codex hook。
- 不设置 repo-local MCP 配置。
- Playwright 只作为任务级可选前端验证工具；不经准入和用户确认，不安装依赖或新增配置。

## 验证基线

完成前报告：

- 修改文件。
- 是否触碰 `run/**`。
- 是否执行删除、清理或移动。
- 是否 staging、commit 或 push。
- 验证命令与结果；无法验证时说明原因。
