# Verification Policy

## Default

- 修改代码必须运行最小有效验证。
- 修改 workflow 脚本必须做 PowerShell parse check。
- 修改文档至少检查 diff、路径引用和禁止路径是否被触碰。
- 无法验证时必须说明具体原因，不得宣称通过。

## Entrypoint

旧 `scripts/workflow/verify.ps1` 已随旧 workflow 主流程移除。验证改为按本轮修改范围选择最小有效命令，例如 PowerShell parse check、JSON parse、定向单元测试、目标子项目测试、`rg` 引用检查和 `git diff --check`。

## Completion Evidence

收尾说明验证命令、实际结果、未验证点、是否触碰 `run/**`、是否删除/清理、是否 commit/push。

## Completion Commit Gate

Claude Code 与 Codex 每次本地执行完毕后，若产生文件修改，必须在最小有效验证完成后自动 commit 本轮修改；不得把提交留作可选收尾步骤。

自动 commit 只允许包含本轮修改文件。提交前必须用 `git status --short`、`git diff --name-status` 和 staged 清单确认无关脏树、历史 staged 内容、用户未授权修改没有进入本轮提交；禁止 `git add .`。

commit 完成后必须再次检查 `git status --short`。若仍有本轮产生的未提交修改，任务不得宣称完成；若只剩进入任务前已经存在的脏树，必须在收尾说明中明确列出并说明未清理原因。

`TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree / finalize / local-review 流程；普通 Codex 修改任务不生成或更新这些文件。
