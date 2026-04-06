# Claude Code 入口 — Hextech

你是 Hextech 工作流执行端，标识符 cc。

固定约束：
- 任务卡使用 agents.md，定位为长期任务总表，不是一次性契约纸
- 运行中若扩范围或新增需求，必须同步更新：
  effective_target_files / effective_modified_symbols / effective_goals / execution_ledger / event_log
- branch_policy = required：必须建分支
- branch_policy = on-demand：默认不建分支；仅当用户主动要求时才建分支
- branch_policy = none：不建分支
- 完工后先更新 PROJECT.md，再进入评审/推送链路
- 合并后本地通过 post-merge 钩子或 .ai_workflow/scripts/post-merge-sync.sh 对齐 standby

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