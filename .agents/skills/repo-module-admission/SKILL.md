---
name: repo-module-admission
description: 仅在准备新增 workflow module、skill、hook 或 tool 前使用；不用于普通编辑、代码审查、维护盘点、记忆/学习、PR、worktree 或任务恢复流程。
---

# repo-module-admission

## 适用场景

准备新增以下长期组件前使用：

- workflow module
- Codex skill
- hook
- tool
- 其他会改变工作流结构的组件

## 输入

- 新增对象名称
- 要解决的问题
- 预期影响范围
- 可能修改的文件
- 最小验证方式

## 输出

- 目标
- 非目标
- 影响范围
- 风险
- 最小验证方式
- 决策：允许 / 拒绝 / 需要人工确认

## 执行规则

- 先判断是否真的需要新增长期组件。
- 优先使用现有 AGENTS.md、现有 skill 或普通 Codex 能力。
- 只有在短规则无法覆盖、且任务会重复出现时，才建议新增。
- 输出必须说明为什么需要新增，以及为什么不能用现有机制替代。

## 禁止事项

- 不直接实现模块。
- 不安装依赖。
- 不修改 hook。
- 不新增 skill，除非用户明确要求。
- 不触碰凭据、token、auth、cookie、API key、proxy 或全局配置。
