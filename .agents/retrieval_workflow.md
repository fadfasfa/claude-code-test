# 检索工作流 — Hextech
> version: 5.1
> 存放路径：`.agents/retrieval_workflow.md`
> 本文件定义网络检索任务的独立链路。
> 检索任务不走代码链路（无分支、无 PR、无 Codex 审查），不使用 agents_template 中的代码字段。

---

## 一、设计原则

检索任务与代码变更任务是两类本质不同的工作：

| 维度 | 代码任务 | 检索任务 |
| :--- | :--- | :--- |
| 产出物 | 代码变更、PR | 回答、摘要、结构化数据 |
| 控制重点 | 文件范围、合并合规、安全 | 来源质量、时效、冲突处理、引用格式 |
| 审计链路 | event_log + PR + Codex | evidence_log + confidence 标注 |
| 版本门控 | Codex PR 审查 | 无，由 confidence_threshold 自控 |

不应把检索任务强塞进代码工作流，否则会出现：检索结果被迫写 PR、event_log 充斥自然语言、真正需要控制的来源质量没有约束。

---

## 二、任务声明格式

开始检索任务前，填写以下结构（可为 YAML 或 Markdown 列表，能被节点解析即可）：

```yaml
task_id:           "retrieval-task-<描述>-<YYYYMMDD>"
task_type:         retrieval
query_plan:
  - "<子查询1>"
  - "<子查询2>"
freshness_required: "<today | this-week | this-month | any>"
source_constraints:
  allowlist:       []     # 留空则不限制；填入时只采信列出的域名
  blocklist:       []     # 强制排除的域名
answer_mode:       "<summary | detail | structured-json>"
citation_required: true
confidence_threshold: 0.8
```

---

## 三、执行协议

### 步骤 1 — 解析任务声明

- 读取 `task_id`、`query_plan`、`freshness_required`、`source_constraints`
- 确认 `citation_required` 和 `confidence_threshold`
- 若 `query_plan` 为空，停止并请求补充

### 步骤 2 — 执行查询

对 `query_plan` 中每条子查询：

1. 发起检索，优先原始来源（官方文档、政府网站、同行评审论文、公司官博），跳过内容农场、纯 SEO 聚合页
2. 若 `freshness_required` 非 `any`，过滤超出时效窗口的来源
3. 若 `source_constraints.allowlist` 非空，只采信白名单域名
4. 向 `evidence_log` 追加条目（见步骤 3）

### 步骤 3 — 记录证据

每条采信的来源追加以下结构到 `evidence_log`：

```yaml
- source_url:   "<URL>"
  domain:       "<域名>"
  retrieved_at: "<yyyy-MM-ddTHH:mm>"
  freshness_ok: true | false
  summary:      "<一两句话，严格改写，不直接引用原文 15 字以上连续段落>"
  confidence:   0.0-1.0
  note:         "<可选备注，如来源冲突>"
```

### 步骤 4 — 冲突处理

若多个来源在同一事实上存在矛盾：

1. 记录到 `conflict_notes`（列出冲突双方及核心分歧）
2. 不得选一个"听起来好的"来源静默忽略另一个
3. 在最终回答中明确指出冲突，并说明采信理由

### 步骤 5 — 置信度自评

对每个关键结论计算综合置信度：

| 置信区间 | 处理方式 |
| :--- | :--- |
| ≥ confidence_threshold | 正常输出 |
| 低于 threshold | 在该结论后标注 `[不确定]` |
| 无法核实 | 明确说"当前无法确认"，给出已知部分 |

禁止用词：总是、绝不、完美、100% 保证、万无一失。

### 步骤 6 — 组织输出

按 `answer_mode` 输出：

**summary**：结论先说（≤3 句），再附来源摘要与冲突说明，最后列引用。

**detail**：结论先说，再逐点展开依据，来源内嵌引用，冲突单独一节说明。

**structured-json**：输出如下格式：

```json
{
  "task_id": "<id>",
  "conclusions": [
    {
      "claim": "<结论>",
      "confidence": 0.9,
      "sources": ["<url1>", "<url2>"]
    }
  ],
  "conflicts": "<冲突说明，或 null>",
  "evidence_log": []
}
```

### 步骤 7 — 引用格式

`citation_required: true` 时，每条具体事实必须标注来源。格式：

```
[来源：<域名或机构名>，<检索日期>]
```

禁止编造来源、链接、论文、机构名或数据。若无法找到可靠来源，说明"未找到可靠来源"。

---

## 四、高时效性查询规则

若 `freshness_required` 为 `today` 或 `this-week`：

- 必须实际查询，不得依赖训练数据给出"当前"结论
- 若无法查询，明确告知用户"当前无法实时核实，以下为训练数据中的已知信息"
- 不得把旧知识包装成最新事实

---

## 五、禁止事项

| 禁止 | 原因 |
| :--- | :--- |
| 编造来源、链接、论文、数据 | 核心可信度要求 |
| 把推断写成已知事实 | 混淆事实与推断 |
| 静默忽略冲突来源 | 掩盖不确定性 |
| 15 字以上连续直接引用 | 版权合规 |
| 一个来源引用超过一次 | 版权合规 |
| 用 `contains` 式宽泛描述替代具体来源 | 证据不可追溯 |

---

## 六、与代码工作流的边界

检索任务完成后：
- 不产生 git commit、不开 PR、不写 runtime_state
- 产出物（回答 + evidence_log）可选择性保存至 `.ai_workflow/retrieval_<task_id>.md`
- 若检索结果需要驱动代码变更，另起新的代码任务并填写独立的 agents.md

---

## 七、任务完成信号

```
[RETRIEVAL: DONE]
task_id:        <id>
queries_run:    <N>
sources_used:   <M>
conflicts:      <有 / 无>
confidence_min: <最低置信度>
output_saved:   <路径，或"未保存">
```
