## 工作范围 (agents.md) — Hextech
> version: 5.3
> status: standby
> 本文件为长期任务总表；待机时保留最近一次合并的归档指针，供下次任务复用。
> retrieval 任务默认不生成代码契约，但可按需写入轻量记录。

---

## 任务头（受控字段）

task_id: none
task_type: code
execution_mode: ad-hoc
branch_policy: on-demand
branch_name: none
base_branch: main
project_path: [待填写]
executor: [待填写]
status: standby
created_at: 2026-04-05T10:37:47Z
last_updated_at: 2026-04-05T10:37:47Z
current_branch: none
current_review_path: none

Task_Mode: standard
Contributors:
  - [待填写]

---

## 初始范围（尽量不改）

initial_target_files:
  - none

initial_modified_symbols:
  - none

initial_goals:
  - [待填写]

---

## 当前有效范围（允许覆盖）

effective_target_files:
  - none

effective_modified_symbols:
  - none

effective_goals:
  - [待填写]

---

## 运行台账（追加式）

execution_ledger:
  - ts: 2026-04-05T10:37:47Z
    type: RESET_TO_STANDBY
    summary: "本地 post-merge-sync 执行后回到 standby（前任务: cx-task-final-auto-merge-verify-20260405）"
    files:
      - none

---

## Decision_Validation

Final_Signer: self
Validation_Sources:
  - none
Validation_Result: skipped
Human_Validation_Required: no
Human_Validation_Reason: none

---

## 待机态（可复用壳）

active_task_id: none
last_merged_task_id: cx-task-final-auto-merge-verify-20260405
last_merged_at: 2026-04-05T10:37:47Z
default_base_branch: main
default_branch_policy_for_ad_hoc: on-demand
merge_archive_path: .ai_workflow/agents_history/local-cx-task-final-auto-merge-verify-20260405.md
