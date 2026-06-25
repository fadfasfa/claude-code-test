---
name: repo-module-admission
description: 仅在准备新增 workflow module、skill、hook、tool、工作区或长期仓库能力前使用；不用于普通编辑、实现、PR 发布或任务恢复。
---

# repo-module-admission

## trigger

准备新增或扩展以下长期组件前使用：

- workflow module
- Codex skill
- hook
- tool
- 工作区 / 子项目注册
- 会改变仓库工作流结构的组件

## scope

- 判断是否真的需要新增长期能力。
- 输出目标、非目标、影响范围、风险、最小验证和决策。
- 优先使用 `AGENTS.md`、现有 skill、普通脚本或一次性命令。
- 涉及工作区、Git 高危操作、验证或 skill 入口时，必须读取 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。
- 默认使用简体中文输出目标、非目标、风险、验证和决策；生成计划文档时正文必须为简体中文，英文计划视为不合格。

## forbidden actions

- 不直接实现模块。
- 不安装依赖。
- 不修改 hook。
- 不触碰凭据、token、auth、cookie、API key、proxy 或全局配置。
- 不新增 skill，除非用户明确要求。

## verification expectation

- 必须说明为什么现有规则或 Codex 能力不够。
- 必须说明新增后的最小验证方式。
- 如果涉及删除、覆盖、移动或备份，必须确认备份成功；备份失败即停止。
