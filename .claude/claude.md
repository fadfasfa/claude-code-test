# Claude Code 入口 — Hextech

你是 Hextech 工作流执行端，标识符 cc。

固定约束：
- `agents.md` 仅作兼容性任务摘要，不再是执行端日常主上下文
- 运行中若扩范围或新增需求，仍需同步更新当前有效范围与事件留痕
- `branch_policy = required`：必须建分支
- `branch_policy = on-demand`：默认不建分支；仅当用户主动要求时才建分支
- `branch_policy = none`：不建分支
- small 任务默认走本地轻量完成
- large 任务默认走 `Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查`
- 完工后先更新 `PROJECT.md`，再进入评审或收口链路
- 本地收口以当前 `completion_mode` 为准，不再把旧式 post-merge 同步当作默认主链路

本端约定：
- 端标识：cc
- 分支前缀：cc-task-
- 状态文件：.ai_workflow/runtime_state_cc.json
- 事件日志：.ai_workflow/event_log_cc.jsonl

读取规则与知识文件：
@../.agents/skills/workflow_registry.md
@../.agents/skills/hextech-workflow.md
@../.agents/skills/decision_playbooks.md
@../.agents/skills/retrieval_workflow.md
@../.agents/contracts/final_review_contract.md
@../.agents/contracts/antigravity_review_contract.md
@../.agents/adapters/codex_review_adapter.md

模板：
@../.agents/skills/agents_template.md
@../.agents/skills/PROJECT_template.md
