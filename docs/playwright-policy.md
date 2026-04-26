# Playwright 策略

Playwright 是 claudecode-only、coding-only 的前端验证能力。

它不进入：

- global core
- `kb`
- global hooks
- 所有任务的默认流程

## 当前发现

Phase 0 发现 PATH 上有 Playwright CLI：

```text
C:\Users\apple\AppData\Local\Programs\Python\Python311\Scripts\playwright.exe
Version 1.58.0
```

未发现 root-level `playwright.config.*`。已有 `package.json` / `package-lock.json` 位于工作区或 legacy worktree 路径，尤其是 `sm2-randomizer\pipeline\collect\wiki`。

没有模块准入卡时，本仓不得安装 Playwright，也不得添加 Playwright configuration/scripts。

## 触发条件

Playwright 只用于：

- 前端任务
- UI interaction 检查
- page behavior 检查
- screenshot validation
- responsive checks
- trace/debug validation
- visual regression 调查

后端、docs、data、repo-governance-only 或 `kb` 任务默认不使用 Playwright，除非用户明确要求。

## 允许范围

- 读取当前任务前端文件。
- 只有本地 URL 或 file URL 可用且任务需要时，才运行浏览器验证。
- screenshot/trace 只写到 `.tmp/` 等 ignored runtime 路径。

## 禁止范围

- 不做全局 Playwright install。
- 未确认前不安装 repo dependency。
- 不写 global hooks。
- 不创建 `kb` validation policy。
- 不启用 Playwright MCP。
- 不做所有任务自动验证。

## 未来 Config 或 Script

任何未来的 `playwright.config.*`、npm script、PowerShell helper 或 hook，都必须先通过 `docs/module-admission.md`。
