## 工作范围 (agents.md) — Hextech
> version: 6.0
> 本模板用于 code 任务；retrieval 任务默认不生成代码契约，但可按需写入轻量记录（见 retrieval_workflow.md）。
> `agents.md` 的定位为"长期任务总表"。

---

## 任务头（受控字段）

task_id: [<端或int>-task-<描述>-<YYYYMMDD>]
task_type: [code]
execution_mode: [contract | ad-hoc]
branch_policy: [required | on-demand | none]
branch_name: [<端或int>-task-<描述>-<YYYYMMDD> | none]
base_branch: [main]
project_path: [相对路径]
executor: [cc | cx | human]
status: [standby | active | paused | review-ready | done-local | merged | archived]
created_at: [yyyy-MM-ddTHH:mm:ss]
last_updated_at: [yyyy-MM-ddTHH:mm:ss]
current_branch: [当前所在分支或 none]
current_review_path: [none | local-only | PR | Gate+PR]

Task_Mode: [standard | dual-end-integration | frontend-integration]
Contributors:
  - [cc / cx / ag / human]

---

## 分支锁（显式独占模型）

branch_lock:
  owner: [cc | cx | human | none]
  status: [free | leased]
  acquired_at: [yyyy-MM-ddTHH:mm:ss | none]
  session_id: [uuid | none]

> 规则：只要某分支已被租约占用（status=leased），另一执行端只能读，不能写。
> 锁操作必须在 execution_ledger 中记录（BRANCH_LOCK_ACQUIRED / BRANCH_LOCK_RELEASED）。

---

## 审查与完工元数据

review_mode: [none | local-only | final-review | gate]
reviewer_role: [human | llm | mixed | none]
completion_mode: [local-main | local-merge | PR-merge]

---

## 任务阻塞状态（并发控制）

blocked_by_task_id: [前置任务 ID | none]
blocked_on_files:
  - [冲突文件路径，或 none]
pause_reason: [FILE_OVERLAP | SESSION_CONFLICT | MANUAL | none]
resume_condition: [predecessor_finished | manual | none]

---

## 初始范围（尽量不改）

initial_target_files:
  - [文件相对路径]

initial_modified_symbols:
  - [文件路径::标识符]
  - [无则填 none]

initial_goals:
  - [最初目标]

---

## 当前有效范围（允许覆盖）

effective_target_files:
  - [文件相对路径]

effective_modified_symbols:
  - [文件路径::标识符]
  - [无则填 none]

effective_goals:
  - [当前仍有效的目标]

---

## 运行台账（追加式）

execution_ledger:
  - ts: [yyyy-MM-ddTHH:mm:ss]
    type: [TASK_CREATED | SCOPE_EXPAND | NEW_REQUIREMENT | PAUSE | RESUME | BRANCH_CREATED | BRANCH_REUSED | ESCALATION | HANDOVER | BRANCH_LOCK_ACQUIRED | BRANCH_LOCK_RELEASED | READY_FOR_REVIEW | TASK_FINISHED | MERGED | RESET_TO_STANDBY | RETRIEVAL_TASK]
    summary: [一句话说明]
    files:
      - [相关文件，或 none]

---

## Planning_Validation（规划签署 — 任务创建时填）

Planning_Signer: [DL-Gemini | self | human]
Planning_Sources:
  - [Claude / GPT / Human / none]
Planning_Result: [agree | conditional | skipped]
Human_Validation_Required: [yes | no]
Human_Validation_Reason: [原因描述，或 none]

---

## Final_Review_Record（完工审查 — 任务完成时填）

Review_Role: [human | llm | mixed | none]
Review_Identity: [codex | claude | ag | user-name | self | none]
Review_Verdict: [PASS | REWORK | skipped]
Review_Timestamp: [yyyy-MM-ddTHH:mm:ss | none]
Review_Signal: [REVIEW-VERDICT: PASS | none]

---

## 待机态（可复用壳）

task_id: none
task_type: code
execution_mode: ad-hoc
branch_policy: on-demand
branch_name: none
base_branch: main
project_path: [待填写]
executor: [待填写]
status: standby
created_at: [待填写]
last_updated_at: [待填写]
current_branch: none
current_review_path: none

Task_Mode: standard
Contributors:
  - [待填写]

branch_lock:
  owner: none
  status: free
  acquired_at: none
  session_id: none

review_mode: none
reviewer_role: none
completion_mode: local-main

blocked_by_task_id: none
blocked_on_files:
  - none
pause_reason: none
resume_condition: none

active_task_id: none
last_completed_task_id: [待填写]
last_completed_at: [待填写]
default_base_branch: main
default_branch_policy_for_ad_hoc: on-demand
archive_path: [待填写]
