# Worktree Policy

本文件只描述当前 worktree 规则。普通 `S` 级任务默认在当前工作树小步执行；`M/L` 级任务按 `AGENTS.md` 的薄边界和当前任务授权执行。

## Trigger

只有以下情况才进入 worktree 流程：

- 用户明确要求开 worktree。
- 上游任务明确标注 `requires_worktree: true`。
- 用户在后续任务中显式提供新的 worktree 创建入口和授权。
- 当前任务已被判为 `M/L`，且当前轮已明确授权按该工作区策略执行。

多文件或多阶段本身不构成开树触发；是否开树以用户授权、上游标记和 `AGENTS.md` 的薄边界为准。

## Managed Roots

- Codex worktree root：`C:\Users\apple\worktrees\codex`
- Claude Code worktree root：`C:\Users\apple\_worktrees\cc`
- 旧 sibling root `C:\Users\apple\claudecode.worktrees` 不再作为默认 root。
- 不混用 CC 与 Codex root；用户明确指定其他 root 时，按高危路径规则先确认目标和写入边界。

## Naming

- 目录名：`<repo>-<agent>-<type>-<slug>`，例如 `claudecode-codex-docs-cleanup` 或 `claudecode-cc-fix-guardrail`。
- 分支名：Codex 使用 `codex/<type>/<slug>`，Claude Code 使用 `cc/<type>/<slug>`。
- `<type>` 使用短分类，例如 `fix`、`feature`、`docs`、`chore`、`probe`。
- `<slug>` 只使用小写字母、数字和 `-`；不得含空格、中文、路径分隔符或 shell 特殊字符。
- 英文分支名沿用本节规则；commit 中需附带的中文别名行格式见 `git-language-policy.md`。

## Create

创建前必须只读检查：

- `git status --short`
- `git worktree list --porcelain`
- 对应 agent 的 managed root
- 目标任务的 `target_work_area`

worktree 写操作按动作分授权位：

- 创建：用户当前轮明确要求新建 worktree 时，该指令本身即构成本地 task worktree 与必要任务分支的创建授权；agent 按命名规则和 managed root 直接创建并验证，不重复要求业务层确认，也不退回手工命令。
- 清理、移除、强制覆盖、修改受管 metadata：仍必须有用户当前轮单独点名的授权；未点名时不动 worktree 已落盘内容，不删除分支，不强制 checkout。
- 任一动作目标超出 managed root、与现有 worktree 路径或分支冲突、或主仓存在与目标重叠的脏改时，停止并报告，不覆盖。

旧 `scripts/workflow/worktree-start.ps1` 已随旧 workflow 主流程移除。`scripts/git/ccw-new.ps1` 已移除，不再作为 active 创建入口。不自动创建 detached worktree，也不默认写入 `TASK_HANDOFF.md` 与 `.task-worktree.json`。

## Checkout Rules

- 默认创建新分支，不在主工作树执行 `checkout` / `switch`。
- base 优先使用用户显式指定值；未指定时按 `origin/HEAD`、当前分支、当前 `HEAD` 依次解析。
- 默认不创建 detached worktree；确需 detached 必须由用户明确要求。
- 目标路径、目标分支或目标 worktree 已存在时停止，不覆盖。
- 若存在 dirty active worktree、目标路径与主仓脏改重叠，或对应 managed root 创建失败，立即停止。

## Metadata And Cleanup

- `TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree 流程。
- 普通任务不生成、不更新这些文件。
- 旧 metadata / cleanup 脚本已随旧 workflow 主流程移除；普通任务不读取或更新 worktree metadata。
- 真实清理前必须确认目标受管且工作树干净，并获得用户显性授权；用户已明确要求清理指定 branch/worktree 时，agent 自行执行并验证。
