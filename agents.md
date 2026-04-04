## 工作范围 (agents.md) — Hextech
> version: 5.3
> 本模板用于 code 任务；retrieval 任务默认不生成代码契约，但可按需写入轻量记录（见 retrieval_workflow.md）。
> `agents.md` 的定位已升级为"长期任务总表"。

---

## 任务头（受控字段）

task_id: [<端或int>-task-<描述>-<YYYYMMDD>]
task_type: [code]
execution_mode: [contract | ad-hoc]
branch_policy: [required | on-demand | none]
branch_name: [<端或int>-task-<描述>-<YYYYMMDD> | none]
base_branch: [main]
project_path: [相对路径]
executor: [cc | cx]
status: [standby | active | review-ready | merged | archived]
created_at: [yyyy-MM-ddTHH:mm:ss]
last_updated_at: [yyyy-MM-ddTHH:mm:ss]
current_branch: [当前所在分支或 none]
current_review_path: [none | local-only | PR | Gate+PR]

Task_Mode: [standard | dual-end-integration | frontend-integration]
Contributors:
  - [cc / cx / ag]

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
    type: [TASK_CREATED | SCOPE_EXPAND | NEW_REQUIREMENT | PAUSE | RESUME | BRANCH_CREATED | BRANCH_REUSED | READY_FOR_REVIEW | MERGED | RESET_TO_STANDBY | RETRIEVAL_TASK]
    summary: [一句话说明]
    files:
      - [相关文件，或 none]

---

## Decision_Validation

Final_Signer: [DL-Gemini | self]
Validation_Sources:
  - [Claude / GPT / Human / none]
Validation_Result: [agree | conditional | skipped]
Human_Validation_Required: [yes | no]
Human_Validation_Reason: [原因描述，或 none]

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

active_task_id: none
last_merged_task_id: [待填写]
last_merged_at: [待填写]
default_base_branch: main
default_branch_policy_for_ad_hoc: on-demand
merge_archive_path: [待填写]
