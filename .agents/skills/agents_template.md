# 兼容性任务摘要模板 — Hextech
> version: 7.1-lite
>
> 本文件仅在明确需要生成临时任务产物或历史兼容输出时使用。
> 默认流程不生成根 `agents.md`，也不把本文件当作常驻主流程依赖。
> small 任务通常不需要；large 任务或 PR 辅助时可写简版。

---

## 任务头

dispatch:
  target_endpoint: [cx | cc | ag]
  target_label: [Codex | Claude Code | Antigravity]

task_id: [<端或int>-task-<描述>-<YYYYMMDD>]
task_type: [code | retrieval]
task_scale: [small | large]
execution_mode: [contract | ad-hoc]
branch_policy: [required | on-demand | none]
completion_mode: [local-main | local-merge | PR-merge]
task_mode: [standard | dual-end-integration | frontend-integration]

---

## 摘要

summary: [一句话说明任务目标]
effective_scope:
  - [当前有效范围]

review_mode: [off | gate | normal]

---

## 备注

- 本文件仅保留轻量任务卡摘要。
- 不记录分支锁、待机壳、阻塞状态、PAUSE/RESUME 或重型台账。
- 不承担执行收口和审查强依赖职责。
