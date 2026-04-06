# 检索工作流 — Hextech
> version: 6.0
> 检索任务默认不走代码链路（无分支、无 PR、无审查）。

---

## 一、设计原则

检索任务重点控制：
- 来源质量
- 时效
- 冲突处理
- 引用格式

---

## 二、与 agents.md 的关系

检索任务仍不生成代码契约，也不写 runtime_state。

但若仓库采用"长期任务总表"模式，可以选择在 `agents.md` 追加一条轻量记录（`retrieval_ledger_entry`）。

该记录仅用于追溯，不进入 PR 审查链路。

---

## 三、retrieval_ledger_entry 最小 schema

在 `agents.md` 的 `execution_ledger` 末尾追加如下条目：

```yaml
- ts: [yyyy-MM-ddTHH:mm:ss]
  type: RETRIEVAL_TASK
  task_id: "retrieval-task-<描述>-<YYYYMMDD>"
  task_type: retrieval
  status: [done | partial | failed]
  query_plan:
    - "<子查询1>"
    - "<子查询2，或 none>"
  sources_used: <N>          # 实际使用的来源数量
  conflicts: <有 | 无>
  confidence_min: <最低置信度，0.0–1.0>
  output_saved: "<路径，或 未保存>"
  summary: "<一句话说明检索结论>"
  files:
    - none                   # retrieval 任务通常不改文件，保持 none
```

字段说明：

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `ts` | 是 | 任务完成时间（UTC ISO 8601）|
| `type` | 是 | 固定为 `RETRIEVAL_TASK` |
| `task_id` | 是 | 全局唯一，格式同任务声明 |
| `task_type` | 是 | 固定为 `retrieval` |
| `status` | 是 | `done` / `partial` / `failed` |
| `query_plan` | 是 | 子查询列表（至少 1 条）|
| `sources_used` | 是 | 整数 |
| `conflicts` | 是 | `有` / `无` |
| `confidence_min` | 是 | 浮点数 |
| `output_saved` | 是 | 文件路径或 `未保存` |
| `summary` | 是 | 一句话结论 |
| `files` | 是 | 无改动时填 `none` |

---

## 四、任务声明格式

```yaml
task_id: "retrieval-task-<描述>-<YYYYMMDD>"
task_type: retrieval
query_plan:
  - "<子查询1>"
freshness_required: "<today | this-week | this-month | any>"
answer_mode: "<summary | detail | structured-json>"
citation_required: true
confidence_threshold: 0.8
```

---

## 五、任务完成信号

任务完成时，必须对比实际最低置信度与声明的 `confidence_threshold`。如果不达标，应输出警告或阻断。

```
[RETRIEVAL: DONE]
task_id:        <id>
queries_run:    <N>
sources_used:   <M>
conflicts:      <有 / 无>
confidence_min: <实际最低置信度>
confidence_threshold_met: <yes / no>
output_saved:   <路径，或"未保存">
```

若 `confidence_threshold_met: no`，执行端必须在 `summary` 或输出报告中补充说明为何未能达标，并建议后续人工复核措施。

若选择写入 `agents.md`，在信号后附加：

```
[AGENTS.MD: SELF-UPDATED]
retrieval_ledger_entry 已追加至 execution_ledger。
```
