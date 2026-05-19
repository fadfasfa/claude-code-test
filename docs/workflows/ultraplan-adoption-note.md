# Ultraplan Adoption Note

## 当前阶段

本阶段只清理旧 CC-CX 强编排和本仓 Guard 运行态，不安装 Ultraplan，不新增复杂编排器。

## 后续阶段

确认 Claude Code on the web 与 `/ultraplan` 可用后，再单独评估接入方式。接入前需要明确：

- 入口命令和可用环境。
- 计划产物落点。
- 与本仓 `git status --short`、最小验证和自动 commit 规则的关系。
- 不读取凭据、不默认发布、不覆盖本地脏树的边界。

## 预期用途

- 复杂任务计划拆解。
- 浏览器审查、页面核验或需要外部交互的计划辅助。
- 跨目录任务的风险枚举和验收清单。

小任务仍由 Claude Code 或 Codex 独立完成，不需要经过 Ultraplan。
