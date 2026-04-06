> **[迁移文档]**
> 本文件记录 V5.3 → V6.0 的迁移要点。

---

# Hextech V5.3 → V6.0 迁移要点

## 一、主轴切换

旧设计（v5.3）：
- 执行端身份强约束 + PR 审查 + 特定 reviewer 信号 + 自动合并 + 合并后待机复位
- 审查绑定 Codex 身份（`codex[bot]`）
- standby reset 唯一入口 = PR merge

新设计（v6.0）：
- **执行身份用于追溯，不用于最终审查准入**
- **最终审查只对变更结果负责，不对执行端品牌负责**
- 统一 finish-task 收尾节点，本地 / PR 均走同一逻辑
- 分支锁显式独占模型

## 二、新增文件

| 文件 | 说明 |
|:---|:---|
| `final_review_contract.md` | 审查者无关的统一审查合同（从 `codex_review_rules.md` 抽象而来）|
| `codex_review_adapter.md` | Codex 专属审查适配器（实现 `final_review_contract.md`）|

## 三、退役文件

| 文件 | 替代 |
|:---|:---|
| `codex_review_rules.md` | 拆为 `final_review_contract.md` + `codex_review_adapter.md` |
| `v5_migration_steps.md` | 本文件 |

## 四、agents_template.md 变更

### 新增字段

| 字段 | 类型 | 说明 |
|:---|:---|:---|
| `branch_lock.owner` | `cc\|cx\|human\|none` | 分支当前占有者 |
| `branch_lock.status` | `free\|leased` | 锁状态 |
| `branch_lock.acquired_at` | timestamp | 租约获取时间 |
| `branch_lock.session_id` | uuid | 防重入标识 |
| `review_mode` | `none\|local-only\|final-review\|gate` | 是否要审 |
| `reviewer_role` | `human\|llm\|mixed\|none` | 谁来审（角色不绑工具名）|
| `completion_mode` | `local-main\|local-merge\|PR-merge` | 怎么完工 |

### 新增状态

| 状态 | 说明 |
|:---|:---|
| `done-local` | 本地完成，不进 PR |

### 重命名字段

| 旧名 | 新名 |
|:---|:---|
| `last_merged_task_id` | `last_completed_task_id` |
| `last_merged_at` | `last_completed_at` |
| `merge_archive_path` | `archive_path` |

### Decision_Validation 拆分

| 旧字段 | 新位置 |
|:---|:---|
| `Final_Signer` | `Planning_Validation.Planning_Signer` |
| `Validation_Sources` | `Planning_Validation.Planning_Sources` |
| `Validation_Result` | `Planning_Validation.Planning_Result` |
| （新增）| `Final_Review_Record.Review_Executor` |
| （新增）| `Final_Review_Record.Review_Verdict` |
| （新增）| `Final_Review_Record.Review_Timestamp` |
| （新增）| `Final_Review_Record.Review_Signal` |

### 新增台账类型

| 类型 | 说明 |
|:---|:---|
| `ESCALATION` | ad-hoc 升格建分支 |
| `HANDOVER` | 执行端移交 |
| `BRANCH_LOCK_ACQUIRED` | 获取分支锁 |
| `BRANCH_LOCK_RELEASED` | 释放分支锁 |
| `TASK_FINISHED` | 任务完成（finish-task 调用时） |

## 五、受控字段分级

旧设计：所有受控字段在 PR diff 中一律禁止修改（与 ad-hoc 升格冲突）

新设计：
- **A 类（真不可变）**：`task_id`、`task_type`、`created_at`、`initial_*`、`project_path`
- **B 类（可受控升级）**：`execution_mode`、`branch_policy`、`branch_name`、`current_review_path`、`status`、`executor`、`review_mode`、`completion_mode`、`branch_lock`
  - B 类变更必须有对应合法台账关联

## 六、审查信号变更

| 旧信号 | 新信号 |
|:---|:---|
| `[AUDIT-VERDICT: PASS]` | `[REVIEW-VERDICT: PASS]` |
| `[AUDIT-DENY: <码>]` | `[REVIEW-VERDICT: DENY <码>]` |

## 七、auto-merge.yml 变更 (现已整体退役)

*(注意：远端 auto-merge.yml 现已退役，目前收尾流程统一收拢本地。如有未来重新启用则按如下规则：)*
- reviewer 身份限制移除（不再检查 `codex[bot]`）
- 改为检查统一信号 `[REVIEW-VERDICT: PASS]`
- PR 描述从"唯一阻断"降级为"可选辅助"（主要通过 agents.md 内容判断）
- standby 壳包含 branch_lock 重置

## 八、Gate 触发条件变更

旧设计：路径命中即触发（单条件）
新设计：路径命中 AND 风险/模式命中（双条件）
- 工作流文档维护不再强制 Gate
- `agents_history/*`、`event_log_*.jsonl` 豁免

## 九、完工路径变更

旧设计：`active → review-ready → merged → archived → standby`（唯一路径）

新设计：
- 大任务 PR 流（已退役）：`active → review-ready → merged → archived → standby`（`PR-merge`）
- 大任务本地合并：`active → review-ready(可选) → done-local → archived → standby`（`local-merge`）
- 小任务本地完成：`active → done-local → archived → standby`（`local-main`）

## 十、并发控制（v6 补充）

### 新增文件

| 文件 | 说明 |
|:---|:---|
| `.git/hextech/active_tasks_index.json` | 全局活跃任务索引，用于文件级冲突发现与 PAUSE/RESUME 调度（存放在非跟踪区防快照漂移） |

### 核心规则

- **文件交集即冲突**：两个活跃任务的 `effective_target_files` 有交集 → 后启动的任务 PAUSE
- **无文件交集才允许并行**
- `effective_modified_symbols` 仅作审查参考，不作为并行准入主条件

### 新增 agents_template.md 字段

| 字段 | 说明 |
|:---|:---|
| `blocked_by_task_id` | 阻塞当前任务的前置任务 ID |
| `blocked_on_files` | 冲突文件列表 |
| `pause_reason` | `FILE_OVERLAP \| SESSION_CONFLICT \| MANUAL \| none` |
| `resume_condition` | `predecessor_finished \| manual \| none` |

### PAUSE/RESUME 协议

- 新任务启动前检查 active_tasks_index 文件交集
- 交集非空 → PAUSE + 记录 blocked_by_task_id
- 前任务 finish-task 时扫描并恢复被阻塞任务

### finish-task 新增步骤

步骤 8：更新 active_tasks_index + 触发 RESUME

### Review_Executor 拆分

| 旧字段 | 新字段 |
|:---|:---|
| `Review_Executor` | `Review_Role`（角色：human/llm/mixed/none）+ `Review_Identity`（可选追溯：codex/claude/ag/user-name/self/none）|

### 新增信号

| 信号 | 含义 |
|:---|:---|
| `[TASK-PAUSED: FILE_OVERLAP with <task_id>]` | 文件冲突导致任务启动时暂停 |
| `[TASK-RESUMED: <task_id>]` | 前任务完成后自动恢复 |
| `[BRANCH-BLOCKED: SESSION_CONFLICT]` | 同执行端不同线程冲突 |
| `[TASK-PAUSED: RUNTIME_OVERLAP with <task_id>]` | 运行中扩范围产生新冲突，自动转入暂停 |

### 新增审查阻断条件

| ID | 说明 |
|:---|:---|
| `TASK_OVERLAP_UNRESOLVED` | 任务应为 paused 但实际提交了变更 |

### workflow_registry.md 新增章节

- **§ K — 并发控制协议**：文件级冲突 SSOT、active_tasks_index schema、启动/恢复/扩范围协议
