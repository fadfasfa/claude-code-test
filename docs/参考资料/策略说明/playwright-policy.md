# Playwright 使用边界

Playwright 是可选的任务级前端验证工具。

它不是：

- 全局核心依赖。
- KB 工作流依赖。
- hook。
- 每个任务都必须执行的检查。

## 当前本机事实

本机 PATH 上存在 Playwright CLI：

```text
C:\Users\apple\AppData\Local\Programs\Python\Python311\Scripts\playwright.exe
Version 1.58.0
```

仓库根目录没有 `playwright.config.*`。

## 仅用于

- 前端任务。
- UI 交互检查。
- 页面行为检查。
- 截图验证。
- 响应式检查。
- trace / debug 验证。
- 视觉回归调查。

## 边界

- 不安装全局 Playwright。
- 不经确认不安装仓库依赖。
- 不经模块准入不新增 Playwright 配置或脚本。
- 不写全局 hook。
- 不默认启用 Playwright MCP。
- 截图或 trace 只写入 `.tmp/` 等已忽略运行时路径。
