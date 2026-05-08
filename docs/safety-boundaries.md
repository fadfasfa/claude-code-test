# 安全边界

本文件定义 `claudecode` 当前 Codex 安全边界。

## 允许的仓库范围

普通仓库任务只允许在 `C:\Users\apple\claudecode\**` 内，并且只在当前任务范围内写入。

除非用户明确纳入范围，不修改：

- `C:\Users\apple\.claude\**`
- `C:\Users\apple\.codex\**`
- `C:\Users\apple\kb\**`
- `.codex/**`
- `run/**`
- `scripts/**`
- 全局 hooks、全局 skills、全局 AGENTS / CLAUDE 文件
- CLI、VS plugin、Codex App、Codex Proxy、Superpowers 或 ECC 安装

不读取或修改凭据、auth 文件、token、cookie、API key、proxy secret 或私有配置。

## 工作区边界

业务修改前，先从 `work_area_registry.md` 选择 `target_work_area`。

目标不清时：

1. 保持只读。
2. 列出候选工作区。
3. 只有无法安全假设时才询问。

## 脏树边界

仓库可能已有无关用户改动。不要回滚、reset、stash、clean 或覆盖它们。

需要 staging 或提交前，先按目的分组 diff，并向用户确认精确文件范围。

## 读取边界

避免大范围读取。先搜索，再打开最小必要上下文。

不检查敏感文件。候选路径看起来像 auth、token、cookie、key、credential、proxy 或 secret 时，停止并报告越界。

## Git 边界

以下操作必须先确认：

- `git add`
- `git commit`
- `git push`
- PR 创建
- `git merge`
- `git reset`
- `git clean`
- `git rebase`
- `git stash`
- 删除分支
- 创建或移除 worktree

除非用户明确要求具体 force 操作，并且已经只读核对目标，否则不使用 force。

## Windows 边界

- 优先使用 PowerShell。
- 使用 Windows 可解析路径。
- 命令输出保持受限、可读。
- 能用补丁或明确文件写入解决时，不做临时脚本式大范围改写。

## KB 边界

KB 仓库有自己的工作流。不要把本仓编码工作流、验证方式、skill 或任务规则推入 KB。
