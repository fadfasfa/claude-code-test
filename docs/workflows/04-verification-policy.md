# Verification Policy

## Default

- 修改代码必须运行最小有效验证。
- 修改 workflow 脚本必须做 PowerShell parse check。
- 修改文档至少检查 diff、路径引用和禁止路径是否被触碰。
- 无法验证时必须说明具体原因，不得宣称通过。

## Entrypoint

优先使用：

```powershell
pwsh -NoProfile -File scripts/workflow/verify.ps1
```

无法自动识别时，脚本应输出原因和建议命令。

## Completion Evidence

收尾说明验证命令、实际结果、未验证点、是否触碰 `run/**`、是否删除/清理、是否 commit/push。

`TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree / finalize / local-review 流程；普通 Codex 修改任务不生成或更新这些文件。
