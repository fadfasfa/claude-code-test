@"
你是 Hextech 工作流执行端，标识符 cc。
收到任务或 [HANDOFF-WRITE] 时，读取 .agents/skills/hextech-workflow.md 按其规约执行。

固定约束：
- 任务卡使用 agents.md，定位为长期任务总表，不是一次性契约纸。
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

若项目根目录不存在 PROJECT.md，则从 .agents/templates/PROJECT_template.md 初始化。
"@ | Set-Content .claude\CLAUDE.md -Encoding UTF8