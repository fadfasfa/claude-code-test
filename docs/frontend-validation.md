# 前端验证

`claudecode` 的前端验证是轻量、任务级流程。它不是完整设计系统，也不是所有任务的默认步骤。

## 什么时候使用

以下任务可以使用本流程：

- 前端 UI 实现
- 页面行为变更
- 交互状态变更
- 响应式或窄屏布局检查
- 视觉回归风险
- 明显可访问性风险
- 需要 screenshot/headed/trace 验证

后端-only、docs-only、data-only 或琐碎文案修改默认不使用，除非用户明确要求。

## 检查项

检查变更后的 UI：

- 视觉层级
- 间距
- 字号和字重
- 色彩对比和 palette drift
- 对齐
- hover / focus / active / disabled 状态
- loading / empty / error 状态
- 移动端和窄屏布局
- 明显可访问性问题

## Playwright 使用

只有 Playwright 能帮助验证当前前端任务时才使用。

允许示例：

```powershell
playwright --version
playwright screenshot http://127.0.0.1:3000 .tmp/frontend-screenshot.png
```

没有模块准入卡和用户确认时，不安装 Playwright，不添加 config，不添加 scripts，也不写 hooks。

## 完成报告

前端完成报告必须包含：

- 改了什么
- 如何验证
- 如使用浏览器 / screenshot / trace，提供对应证据
- 是否仍需要人工视觉确认

检查清单使用 `frontend-polish-lite` skill。
