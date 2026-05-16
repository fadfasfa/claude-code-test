# 前端验证

前端验证按任务触发，不是设计系统，也不是每次修改都必须执行。

## 适用场景

- UI 实现变化。
- 页面行为变化。
- 交互状态变化。
- 响应式布局有风险。
- 视觉回归有风险。
- 明显可访问性风险。
- 截图、headed run 或 trace 能显著提高信心。

纯文档、纯后端、纯数据和微小文案修改不需要前端验证，除非用户要求。

## 检查点

- 视觉层级。
- 间距。
- 字体。
- 色彩对比。
- 对齐。
- hover / focus / active / disabled 状态。
- loading / empty / error 状态。
- 移动端和窄屏布局。
- 明显可访问性问题。

## Playwright

仅在当前前端任务确实需要时使用 Playwright。

示例：

```powershell
playwright --version
playwright screenshot http://127.0.0.1:3000 .tmp/frontend-screenshot.png
```

不经模块准入和用户确认，不安装 Playwright、不新增配置、不新增脚本、不写 hook。

## 报告

前端完成报告应说明改了什么、如何验证、是否仍有视觉风险。
