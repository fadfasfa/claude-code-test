# Worktree Policy

本文件只描述当前 worktree 规则。普通任务默认在当前工作树小步执行，不自动创建分支或 worktree；worktree 是显式授权的执行面。

## Trigger

只有以下情况才进入 worktree 流程：

- 用户明确要求开 worktree。
- 上游任务明确标注 `requires_worktree: true`。
- 用户在后续任务中显式提供新的 worktree 创建入口和授权。

多文件、多阶段、高风险或 non-trivial 本身都不构成开树触发。

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

## Create

创建前必须只读检查：

- `git status --short`
- `git worktree list --porcelain`
- 对应 agent 的 managed root
- 目标任务的 `target_work_area`

创建、清理或修改 worktree 属于 worktree 写操作，必须先输出计划并等待用户确认；计划必须包含 `git status`、目标 worktree 路径、预计修改文件、修改内容、不修改范围、验证命令和 Git 处理方式。

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
- 真实清理前必须确认目标受管且工作树干净，并获得用户显性授权。
