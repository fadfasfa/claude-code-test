# Full Workflow Bootstrap v1

本目录记录 `claudecode` 作为个人总编程仓的可执行工作流骨架。

## 目标

- 让任务从入口、工作区选择、验证、审查到发布准备都有固定路径。
- 保护业务数据、密钥和不可重建资产。
- 保持根目录为治理层，不承载子项目业务规则。

## 默认流程

1. 读 `AGENTS.md`、`PROJECT.md`、`work_area_registry.md`。
2. 选择明确 `target_work_area`。
3. 使用单一 active worktree。
4. 小步修改，避免顺手重构。
5. 运行 `scripts/workflow/verify.ps1` 或说明无法验证的原因。
6. 使用 `scripts/workflow/local-review.ps1` 做本地审查摘要。
7. 发布类动作必须由用户明确授权。

## 停止条件

- 目标路径不清。
- 涉及凭据、token、cookie、proxy secret 或私有配置。
- 备份失败。
- 验证命令无法运行且没有明确原因。
- 将要触碰未授权业务工作区或当前脏树。
