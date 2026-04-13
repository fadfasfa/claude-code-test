# 检索工作流 — Hextech
> version: 7.1-lite
> 检索任务默认不走代码链路（无分支、无 PR、无审查）。
> 检索任务只产出结果，不进入任何代码任务台账，也不生成根 `agents.md`。

---

## 一、设计原则

检索任务重点控制：
- 来源质量
- 时效
- 冲突处理
- 引用格式

---

## 二、任务声明格式

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

## 三、任务完成信号

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

---

## 四、Case Validation 模式

适用场景：
- 正式契约前，需要让外部 AI 搜索网络成熟案例
- 需要比较多条实现路径的常见性与稳妥程度
- 需要确认是否存在行业常见做法、安全基线、降级策略

任务声明格式追加字段：

```yaml
validation_focus:
  - "<待验证问题1>"
  - "<待验证问题2>"
case_search_scope:
  - "官方文档"
  - "成熟开源项目"
  - "框架最佳实践"
comparison_required: true
```

外部 AI 返回结果必须包含：
- feasibility_verdict: [可行 | 条件可行 | 不建议]
- common_pattern_found: [yes | no]
- mature_case_summary:
  - "<案例1>"
  - "<案例2>"
- recommended_path: "<推荐路径>"
- reasons:
  - "<原因1>"
  - "<原因2>"
- risks:
  - "<风险1>"
  - "<风险2>"
- confidence: <0.0-1.0>
- citations_required: true
