# Verification Policy

## 默认要求

- 修改代码必须运行最小有效验证。
- 修改工作流脚本必须做 PowerShell 语法检查。
- 修改文档至少检查路径、diff 和禁止路径是否被触碰。
- 无法验证时必须说明具体原因，不得宣称通过。

## 入口

优先使用：

```powershell
pwsh -NoProfile -File scripts/workflow/verify.ps1
```

无法自动识别时，脚本应输出原因和建议命令。

## 完成前

- 列出验证命令。
- 列出实际结果。
- 列出未验证点。
- 列出是否触碰 `run/**`、是否删除/清理、是否提交或推送。
