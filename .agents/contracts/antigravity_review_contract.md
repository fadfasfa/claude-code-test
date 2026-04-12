# Antigravity 审查契约 — Hextech
> version: 7.1-lite
> 存放路径：`.agents/contracts/antigravity_review_contract.md`
> Antigravity 的定位：高难前端执行端 / 前端专项审查 / 周期性大型审计节点。
> 它不是常规执行端，不参与日常 small 任务和普通后端执行。
> Gate Mode 触发条件的唯一权威来源为 `workflow_registry.md § G`（双条件模型），本文件只引用，不自行扩展。

---

## 一、运行模式

| 模式 | 触发条件 | 是否阻断 PR | 输出格式 |
| :--- | :--- | :--- | :--- |
| **Advisory Mode** | 人类随时主动触发，粘贴代码或文件路径 | 否 | 自由格式，给出分级建议 |
| **Gate Mode** | 见下方触发条件，强制执行 | 是（未输出 `[AG-REVIEW-PASS]` 时审查端拒绝 PR） | 必须符合本契约输出格式 |

---

## 二、Gate Mode 触发条件

### 强制触发（满足任一即触发）

| 条件 | 说明 |
| :--- | :--- |
| `task_mode: dual-end-integration` | cc + cx 双端都修改过同一 workflow，需要跨端一致性审查 |
| `task_mode: frontend-integration` + 重要前端场景 | 见下方定义；普通样式调整或文案修改不触发 |
| `task_mode: standard` + 双条件均满足 | 命中 `workflow_registry.md § G` 中的双条件模型 |
| 人类主动指定 `review_mode: gate` | 人类显式要求 Antigravity 审查 |
| 周期性大型审计 | 由人类主动发起，用于跨任务的系统级审查 |

### 重要前端场景定义

以下情况属于“重要前端场景”，`task_mode: frontend-integration` 时须触发 Gate：

- 前端代码涉及鉴权 / 权限展示逻辑
- 前端代码修改了共享组件或公共布局
- 前端代码接入了新的后端接口或数据契约
- 人类认为该前端改动影响范围超出单一页面 / 单一功能

以下情况不触发 Gate（即使 `task_mode: frontend-integration`）：

- 纯样式调整
- 文案修改
- 静态内容替换
- 单一组件的小幅视觉迭代，且无状态变更

---

## 三、Gate Mode 输入

```
[AG-REVIEW-REQUEST]
task_id: <id>
task_mode: standard | dual-end-integration | frontend-integration
execution_mode: contract | ad-hoc
branch_policy: required | on-demand | none
review_mode: gate
diff_summary: <git diff --stat 输出>
task_context: <任务说明 / PR 描述 / 前端接入背景>
review_focus:
  - <需重点关注的模块或接口>
```

> `task_context`：当 `task_mode: frontend-integration` 时，需包含本次前端接入背景与触发 Gate 的具体原因。

---

## 四、Gate Mode 输出格式

```
[AG-REVIEW-RESULT]
task_id: <id>
reviewer: Antigravity
ts: <yyyy-MM-ddTHH:mm:ss>
verdict: PASS | REWORK
risk_summary: <一句话>
```

### 4.1 PASS 信号

```
[AG-REVIEW-PASS]
task_id: <id>
verdict: PASS
可继续推送 PR，审查端将接受此报告作为 Gate 证据。
```

### 4.2 REWORK 信号

```
[AG-REVIEW-REWORK]
task_id: <id>
verdict: REWORK
block_reasons:
  - <问题1>
  - <问题2>
```

---

## 五、角色边界说明

Antigravity **是**：
- 高难前端执行端
- 前端专项审查节点
- 由人类发起的周期性大型系统审计角色

Antigravity **不是**：
- 常规执行端
- 普通后端执行端
- 广义 Gate 节点
