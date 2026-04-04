# Antigravity 审查契约 — Hextech
> version: 5.3
> 存放路径：`.agents/antigravity_review_contract.md`（Git 跟踪，版本管理）
> 本文件定义 Antigravity 在 Gate Mode 下的输入、输出、严重级别和阻断条件。
> Advisory Mode（日常建议扫描）不受本契约约束，自由格式输出即可。
> **Gate Mode 触发条件的唯一权威来源为 `workflow_registry.md § G`，本文件引用不自行扩展。**

---

## 一、两种运行模式

| 模式 | 触发条件 | 是否阻断 PR | 输出格式 |
| :--- | :--- | :--- | :--- |
| **Advisory Mode** | 人类随时主动触发，粘贴代码或文件路径 | 否 | 自由格式，给出分级建议 |
| **Gate Mode** | 见下方触发条件，强制执行 | 是（未输出 `[AG-REVIEW-PASS]` 时 Codex 拒绝 PR）| 必须符合本契约输出格式 |

---

## 二、Gate Mode 触发条件（满足任一即触发）

| 条件 | 说明 |
| :--- | :--- |
| `task_mode: dual-end-integration` | cc + cx 双端都修改过同一 workflow，需要跨端行为一致性审查 |
| `task_mode: frontend-integration` | Antigravity 生成的前端代码即将纳入主项目（一律强制，无 risk_level 豁免）|
| `task_mode: standard` | 命中 `workflow_registry.md § G` 中定义的 `standard_gate_trigger_conditions` 路径白名单时自动触发 |

---

## 三、Gate Mode 输入（Antigravity 审查前人类提供）

```
[AG-REVIEW-REQUEST]
workflow_id: <id>
branch_role: execution | integration
integration_branch: <int-task-xxx 或 N/A>
contributors: [cc] / [cx] / [cc,cx] / [ag,cc] / [ag,cx]
task_mode: standard | dual-end-integration | frontend-integration
execution_mode: contract | ad-hoc
branch_policy: required | on-demand | none
agents_md: <agents.md 完整内容>
cc_event_log: <event_log_cc.jsonl 完整内容，或 N/A>
cx_event_log: <event_log_cx.jsonl 完整内容，或 N/A>
diff_summary: <git diff --stat 输出>
review_focus:
  - <需重点关注的模块或接口>
```

> Antigravity 在 Gate Mode 下默认以 `agents.md` 中的"当前有效范围"为第一审查入口，
> 再用 `execution_ledger` 与 event_log 校验演进历史。

---

## 四、Gate Mode 输出格式（必须严格遵守）

```
[AG-REVIEW-RESULT]
workflow_id: <id>
reviewer: Antigravity
ts: <yyyy-MM-ddTHH:mm:ss>
verdict: PASS | REWORK
risk_summary: <一句话>

── 高危（BLOCK）──
[HIGH] <文件:行号> <类型>
  描述：<说明>
  跨端影响：<cc 改了 X，cx 改了 Y，合并后会 Z>
  修复：<建议>

── 中危（WARN）──
[MEDIUM] ...

── 低危（INFO）──
[LOW] ...

── 任务卡一致性检查 ──
初始范围：<说明>
当前有效范围：<说明>
运行台账摘要：<说明>
event_log 一致性：<一致 / 不一致，说明原因>

── 跨端一致性检查 ──
cc 修改范围：<列出>
cx 修改范围：<列出>
重叠或冲突：<说明，或"无">
合并后行为评估：<说明>

── 前端专项（task_mode=frontend-integration 时）──
前端代码来源：Antigravity 生成
纳入执行端：<cc | cx>
浏览器验证方式：<方式，或"占位通过">
已知局限：<说明>

── 总结 ──
高危 <N> 个 / 中危 <M> 个 / 低危 <K> 个
```

### 4.1 PASS 信号

无高危问题时，在输出末尾追加：

```
[AG-REVIEW-PASS]
workflow_id: <id>
verdict: PASS
可继续推送 PR，Codex 将接受此报告作为 Gate 证据。
```

### 4.2 REWORK 信号

存在高危问题时，在输出末尾追加：

```
[AG-REVIEW-REWORK]
workflow_id: <id>
verdict: REWORK
block_reasons:
  - <问题1>
  - <问题2>
执行端须修复上述问题后，重新触发 Gate Mode 审查。
```

---

## 五、严重级别定义

| 级别 | 含义 | 对 PR 的影响 |
| :--- | :--- | :--- |
| HIGH | 合并后会破坏功能或引入安全漏洞 | 阻断，verdict = REWORK |
| MEDIUM | 潜在风险或代码质量问题 | 不阻断，PASS 时记入告警 |
| LOW | 建议性改进 | 不阻断，信息性记录 |

---

## 六、Codex 如何使用此报告

在 PR 审查第二步（Antigravity Gate 前置检查时）：

1. 若 `task_mode` 为 `dual-end-integration` 或 `frontend-integration`：
   - 从 PR 描述的 `antigravity_report_path` 读取报告
   - 检查报告中是否包含 `[AG-REVIEW-PASS]`
   - 不包含 → 输出 `[AUDIT-DENY: AG-REVIEW-MISSING]`，Request Changes

2. 报告存在且包含 `[AG-REVIEW-PASS]` → 继续正常审查流程，不重复 Antigravity 已覆盖的跨端检查

---

## 七、报告存储路径

Gate Mode 审查完成后，人类将 Antigravity 输出保存至：

```
.ai_workflow/ag_review_<workflow_id>.md
```

并在 PR 描述中填写 `antigravity_report_path: .ai_workflow/ag_review_<workflow_id>.md`。
