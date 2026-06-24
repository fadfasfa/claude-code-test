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

业务修改前，先从 `docs/当前规则/10-工作区登记.md` 选择 `target_work_area`。

目标不清时：

1. 保持只读。
2. 列出候选工作区。
3. 只有无法安全假设时才询问。

## 脏树边界

仓库可能已有无关用户改动。不要回滚、reset、stash、clean 或覆盖它们。

需要 staging 或提交前，先按目的分组 diff，并确认没有超出当前轮明确授权或已批准计划的精确文件范围。

## 读取边界

避免大范围读取。先搜索，再打开最小必要上下文。

不检查敏感文件。候选路径看起来像 auth、token、cookie、key、credential、proxy 或 secret 时，停止并报告越界。

## Git 边界

以下操作必须有当前轮明确授权或已批准计划：

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
- 移除、清理、覆盖或强制 checkout 已有 worktree

未明确授权时禁止主动执行 push、PR、merge 或 discard；用户明确要求后由 agent 按确认范围完整执行并验证结果，不得要求用户手动输入命令。

用户当前轮明确要求新建 worktree 时，该指令本身即构成本地 task worktree 与必要任务分支的创建授权；agent 按 `docs/当前规则/20-Git与高危操作.md` 的 managed root 和命名规则直接创建并验证，不重复要求业务层确认。移除、清理或强制覆盖 worktree 仍属于上方列表，必须用户单独点名。

除非用户明确要求具体 force 操作，并且已经只读核对目标，否则不使用 force。

## Windows 边界

- 优先使用 PowerShell。
- 使用 Windows 可解析路径。
- 命令输出保持受限、可读。
- 能用补丁或明确文件写入解决时，不做临时脚本式大范围改写。

## KB 边界

KB 仓库有自己的工作流。不要把本仓编码工作流、验证方式、skill 或任务规则推入 KB。

