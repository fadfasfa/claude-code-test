# 工作流注册表 — Hextech
> version: 7.1-lite
> 单一事实来源（SSOT）。

---

## § A — 执行端标识与分支前缀

> executor 及分支前缀为追溯元数据，不作为最终审查硬门槛。

| 执行端 | 端标识 | 推荐分支前缀 | 状态文件 | Event Log |
| :--- | :--- | :--- | :--- | :--- |
| Claude Code | `cc` | `cc-task-` | `.ai_workflow/runtime_state_cc.json` | `.ai_workflow/event_log_cc.jsonl` |
| Codex | `cx` | `cx-task-` | `.ai_workflow/runtime_state_cx.json` | `.ai_workflow/event_log_cx.jsonl` |
| Antigravity | `ag` | `ag-task-` | 不适用 或 `.ai_workflow/runtime_state_ag.json` | 不适用 或 `.ai_workflow/event_log_ag.jsonl` |
| 人工 | `human` | `task-` 或任意 | 不适用 | 不适用 |
| 集成 | `int` | `int-task-` | 不适用 | 不适用 |

> 前缀不匹配时审查层仅输出 `[REVIEW-WARN: BRANCH_PREFIX_INFO]`，不阻断。

---

## § A.1 — 执行端角色说明

| 执行端 | 定位 |
| :--- | :--- |
| Codex | 主执行端；大任务 PR 唯一自动审查入口 |
| Antigravity | 高难前端执行端 + 前端专项审查 + 周期性大型审计 |
| Claude / Claude Code | 文件生成接口；承接文件级改动说明（File Change Spec）输出 |

> Antigravity 不承接普通后端执行或普通小任务 review。

---

## § B — 附属白名单

```
.ai_workflow/*.json
.ai_workflow/*.jsonl
.ai_workflow/agents_history/*.md
PROJECT.md
PROJECT_history.md
```

---

## § C — .ai_workflow/runtime_state_<端>.json

```json
{
  "schema_version": "7.1-lite",
  "endpoint": "<cc | cx>",
  "workflow_id": "",
  "task_type": "<code>",
  "execution_status": "idle",
  "retry_count": 0,
  "is_new_project": false,
  "interface_summary": {
    "modified_symbols": [],
    "affected_files": []
  }
}
```

retrieval 任务默认不写 runtime_state。

---

## § E.1 — 前置阶段

| 阶段 | 含义 | 适用任务规模 |
| :--- | :--- | :--- |
| `requirement-refine` | 多轮需求精炼，未形成可签发任务 | large（small 任务可跳过）|
| `plan-draft` | 已读取 PROJECT.md 和代码现状，形成文件级计划稿，等待交叉验证与执行器定稿 | large（必须经过此阶段）|
| `validation-draft` | 正式契约前的交叉验证稿（以 Plan Draft 为输入底稿）| large（满足条件时）|
| `handoff` | 已满足签发条件，输出正式契约 | large + small |

### plan-draft 阶段说明

Plan Draft 是大型任务的必经中间层，介于 validation-draft 和正式契约之间：

- **输入**：`PROJECT.md` + 本地代码现状（通过 GitNexus）+ 需求精炼结论
- **输出**：文件级计划稿（背景 / 现状 / 涉及文件 / 改法 / 风险 / 交叉验证问题清单）
- **信号**：`[PLAN-DRAFT: READY]`
- **可传递给**：GPT / Claude 做交叉验证，或直接传给 Codex plan mode

---

## § E — 三轴路由表

### 1. task_type

| task_type | 含义 |
| :--- | :--- |
| `code` | 代码修改任务 |
| `retrieval` | 检索任务 |

### 2. task_scale（v7.1-lite）

| task_scale | 含义 | 决策端行为 |
| :--- | :--- | :--- |
| `small` | 小型任务（≤3 文件，无跨模块影响）| 伴随式指导 + 局部验收，不阻断 Codex |
| `large` | 大型任务（≥4 文件，或有架构/接口影响）| 规划中枢：读项目 → Plan Draft → 验证 → 派工 |

### 3. execution_mode

| execution_mode | 含义 |
| :--- | :--- |
| `contract` | 正式契约任务 |
| `ad-hoc` | 非契约代码任务，仍可写兼容性任务摘要 |

### 4. branch_policy

| branch_policy | 含义 |
| :--- | :--- |
| `required` | 必须新建分支 |
| `on-demand` | 默认不建分支；**仅用户主动要求时**才升格建分支 |
| `none` | 不建分支 |

### 5. completion_mode

| completion_mode | 含义 |
| :--- | :--- |
| `local-main` | 本地 main 直接完成（**小任务默认**）|
| `local-merge` | 本地分支手动合并后完成（大任务可选降级）|
| `PR-merge` | 远端 PR merge 后完成（**大任务默认**）|

### 6. 推荐组合（v7.1-lite）

| 任务场景 | task_type | task_scale | execution_mode | branch_policy | completion_mode |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 正式代码任务（大任务）| `code` | `large` | `contract` | `required` | `PR-merge`（**默认**；无远端条件时可降级 `local-merge`）|
| 非契约小任务 | `code` | `small` | `ad-hoc` | `on-demand` | `local-main`（**默认**）|
| 检索任务 | `retrieval` | — | `ad-hoc` | `none` | `local-main` |

> **大任务 PR 是 Codex 自动审查的唯一入口**；若降级为本地合并或本地完成，只需在任务说明或 PROJECT.md 中记录原因，不再依赖旧台账。

---

## § F — 信号

| 信号 | 含义 |
| :--- | :--- |
| `[AGENTS.MD: UPDATED]` | 当前有效范围或兼容性摘要已更新 |
| `[AGENTS.MD: SELF-UPDATED]` | 自助模式下已同步更新当前有效范围和兼容性摘要 |
| `[PLAN-DRAFT: READY]` | 计划稿已形成，可传给验证器或 Codex plan mode |
| `[VALIDATION-DRAFT: READY]` | 验证稿已形成，可转发给其他 AI |
| `[VALIDATION: PARTIAL]` | 已收到部分验证结果，但仍存在冲突或缺口 |
| `[VALIDATION: PASSED]` | 交叉验证已达到签约门槛 |
| `[VALIDATION-BLOCKED]` | 外部验证发现明显阻断，不得签发正式契约 |
| `[HANDOFF-BLOCKED]` | handoff 校验失败或前置依赖未满足，外显缺失项并阻止任务卡渲染 |
| `[BRANCH: DEFERRED]` | 当前任务默认不建分支（ad-hoc，等待用户主动要求）|
| `[BRANCH: ESCALATED]` | 用户主动要求，已升格建分支 |
| `[RISK-WARN: SENSITIVE_CHANGE]` | 检测到敏感路径变更，告知用户但不自动升格分支 |
| `[REVIEW-VERDICT: PASS]` | 最终审查通过 |
| `[REVIEW-VERDICT: DENY <原因码>]` | 最终审查不通过 |
| `[REVIEW-WARN: <类型>]` | 审查告警（不阻断）|
| `[FILE-CHANGE-SPEC: READY]` | 文件级改动说明已输出，可传给 Claude / Claude Code |

### § F.1 — 信号绑定规则

以下信号用于内部状态约束；除非本节另有说明，成功态默认不外显：

1. `execution_mode = ad-hoc` 且 `branch_policy = on-demand`
   - 默认内部记为 deferred
   - 成功态不要求外显 `[BRANCH: DEFERRED]`
   - 仅在用户追问分支策略、字段缺失、分支冲突时外显
2. 用户主动要求建分支 / PR / push / 发起评审
   - 必须同步更新相关字段，并外显 `[BRANCH: ESCALATED]`
3. `[DL-VALIDATION-PASS]`
   - 仅作为内部验证状态，不要求在成功态对话中显示
4. 若 handoff 校验未通过
   - 外显 `[HANDOFF-BLOCKED]`
   - 列出缺失项
   - 禁止输出任务卡代码块
5. `review_mode = gate`
   - 必须满足 Gate 触发条件或显式人工指定，不得凭习惯进入

#### 成功态输出最小化规范
- 成功态：只输出"请粘贴到：<目标端口>" + 单个 yaml 代码块
- 失败态：只输出 `[HANDOFF-BLOCKED]` + 缺失项/阻断项

---

## § G — Task_Mode 定义

| Task_Mode | 含义 | Antigravity Gate | 集成分支 |
| :--- | :--- | :--- | :--- |
| `standard` | 单端执行 | 双条件触发（见下表）| 否 |
| `dual-end-integration` | 双端都修改 | 是 | 是 |
| `frontend-integration` | Antigravity 生成前端，执行端纳入 | 是 | 否 |

### Gate 触发条件（双条件模型）

`task_mode = standard` 的任务必须**同时满足以下两个条件**才强制进入 Gate Mode：

**条件一（路径命中）** — 当前变更结果（本地 git diff 或 PR diff）包含以下任一路径的修改：

1. **依赖与环境**
   - `**/requirements.txt`, `**/pyproject.toml`, `**/Pipfile*`
   - `**/package.json`, `**/yarn.lock`, `**/package-lock.json`
   - `**/Dockerfile`, `**/.dockerignore`
2. **CI/CD 与工作流配置**
   - `.github/workflows/**`, `.gitlab-ci.yml`
   - `.ai_workflow/**`（排除 `agents_history/*`、`event_log_*.jsonl`）
3. **安全与系统级配置**
   - `**/settings.py`, `**/config.py`, `**/manage.py`
   - `**/.env*`, `**/secrets*`
4. **鉴权与核心扩展**
   - 路径命中 `**/auth/**`, `**/security/**`, `**/permissions/**` 等敏感域
   - 中间件配置、全局路由注册表

**条件二（风险或模式命中）** — 满足以下任一：

- `review_mode = gate`
- 命中安全检测（SEC-00x）
- 涉及鉴权/权限变更

> `dual-end-integration` 和 `frontend-integration` 始终强制 Gate，不适用双条件。

### Gate 豁免（即使路径命中条件一）

以下路径的修改不计入条件一：
- `.ai_workflow/agents_history/*`
- `.ai_workflow/event_log_*.jsonl`
- `.ai_workflow/` 下的 markdown 文档（流程维护）

---

## § H — agents.md 状态枚举

> **v7.1-lite 定位变更**：agents.md 降级为兼容性任务摘要。small 任务默认不要求写；large 任务或进 PR 时可选写简版。审查不再因未写 agents.md 而 deny。

| 状态 | 含义 |
| :--- | :--- |
| `standby` | 待机，可复用壳 |
| `active` | 正在执行 |
| `review-ready` | 已准备进入评审链路（仅 PR 流用到）|
| `done-local` | 本地完成，不进 PR |
| `merged` | 已合并（仅 PR 流用到）|
| `archived` | 已归档 |

### 三条主干完工路径

| 路径 | 状态流转 | completion_mode |
|:---|:---|:---|
| 大任务分支 + PR（**默认**）| `standby → active → review-ready → merged → archived` | `PR-merge` |
| 大任务分支 + 本地合并（可选降级）| `standby → active → review-ready(可选) → done-local → archived` | `local-merge` |
| 小任务 main 直接完成（**默认**）| `standby → active → done-local → archived` | `local-main` |

---

## § I — 本地同步工件

| 工件 | 路径 | 用途 |
| :--- | :--- | :--- |
| 大任务规划稿 | `.ai_workflow/plan_draft_<task_id>.md` | Plan Draft 阶段产物 |
| 文件改动说明 | `.ai_workflow/file_change_spec_<task_id>.md` | File Change Spec 产物 |
| 历史归档 | `.ai_workflow/agents_history/<task_id>.md` | 已完成任务摘要存档 |
