<!-- claudecode-repo-local -->
---
name: frontend-polish-lite
description: Lightweight claudecode-only frontend UI polish and validation for visual, responsive, interaction, and accessibility smoke checks.
---

# frontend-polish-lite

中文简介：本 skill 是 claudecode-only 的轻量前端微调与验收入口。它用于视觉、响应式、交互状态和 accessibility smoke checks；不负责完整 design system、产品重设计、依赖安装或所有任务默认视觉 QA。

## 什么时候使用

当前端任务需要 UI polish 或 validation 时使用。

## 检查范围

检查：

- visual hierarchy
- spacing
- typography
- color and contrast
- alignment
- hover / focus / active / disabled states
- loading / empty / error states
- mobile and narrow-screen layout
- obvious accessibility issues

## Playwright

只有 Playwright 能帮助当前任务时才使用。

允许：

- `playwright --version`
- 对 local URL 或 file URL 做 screenshot / headed / trace validation
- 将截图或 trace 写到 ignored `.tmp/`

没有模块准入卡和用户确认时，禁止：

- 安装 Playwright
- 添加 `playwright.config.*`
- 添加 npm scripts 或 validation scripts
- 添加 hooks
- 添加 MCP
- 让 Playwright 成为所有任务的默认步骤

## 规则

- 本 skill 只属于 `claudecode`。
- 不应用到 `kb`。
- 不写 global hooks 或 global skills。
- 不把轻量 polish pass 变成完整 design system。
- 用户未要求时不重设计产品。

## 完成报告

包含：

- 改了什么
- 如何验证
- 是否使用 Playwright
- 是否仍需要人工视觉确认
