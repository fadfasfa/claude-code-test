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

`TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree / finalize / local-review 流程；普通 Codex 修改任务不生成或更新这些文件。
