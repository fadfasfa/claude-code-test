# 最终审查合同 — Hextech
> version: 6.0
> 本文件定义审查标准，不绑定任何特定审查执行器。
> 任何 reviewer（Codex / Claude / Antigravity / 人工）均须实现本合同。

---

## 一、主原则

**执行身份用于追溯，不用于最终审查准入；最终审查只对变更结果负责，不对执行端品牌负责。**

---

## 二、审查输入（必须提供）

| 输入 | 来源 | 必须 |
|:---|:---|:---|
| `diff` | PR diff 或本地 `git diff` | 是 |
| `effective_scope` | `agents.md` 当前有效范围（`effective_target_files` / `effective_modified_symbols` / `effective_goals`）| 是 |
| `execution_ledger` | `agents.md` 运行台账 | 是 |
| `event_log` | `.ai_workflow/event_log_<端>.jsonl`（如有）| 否（无分支时可无）|
| `branch_lock_state` | `agents.md` 分支锁状态（如有分支）| 否 |
| `active_tasks_index` | `.git/hextech/active_tasks_index.json`（检查未解决的文件重叠）| code 任务默认必需，retrieval 可无 |

读取顺序固定为：

1. **本次变更后的** `agents.md` 的 `当前有效范围`
2. **本次变更后的** `agents.md` 的 `execution_ledger`
3. `event_log`（如有）
4. 三者一致性与时序逻辑检验

---

## 三、受控字段分级

### A 类 — 真正不可变

变更即阻断，无例外。

| 字段 | 理由 |
|:---|:---|
| `task_id` | 任务唯一标识 |
| `task_type` | 任务类型不应运行期变化 |
| `created_at` | 创建时间戳 |
| `initial_target_files` | 初始快照 |
| `initial_modified_symbols` | 初始快照 |
| `initial_goals` | 初始快照 |
| `project_path` | 项目路径 |

### B 类 — 可受控升级

允许变更，但必须有对应合法台账关联。

| 字段 | 要求的台账条目类型 |
|:---|:---|
| `execution_mode` | `SCOPE_EXPAND` 或 `ESCALATION` |
| `branch_policy` | `BRANCH_CREATED` 或 `ESCALATION` |
| `branch_name` | `BRANCH_CREATED` 或 `ESCALATION` |
| `current_review_path` | `READY_FOR_REVIEW` |
| `status` | 顺序合法（参照状态流转表）|
| `executor` | `HANDOVER`（仅移交场景）|
| `review_mode` | 随 `branch_policy` 同步升级 |
| `completion_mode` | 随实际完工路径更新 |
| `branch_lock` | 锁操作本身产生台账记录（`BRANCH_LOCK_ACQUIRED` / `BRANCH_LOCK_RELEASED`）|

---

## 四、硬阻断条件

命中任一 → 输出 `[REVIEW-VERDICT: DENY <原因码>]`。

| ID | 规则 |
|:---|:---|
| `IMMUTABLE_MODIFIED` | A 类受控字段在 diff 中被修改 |
| `CONTROLLED_UNLOGGED` | B 类受控字段在 diff 中被修改但无对应合法台账 |
| `SCOPE_VIOLATION` | diff 文件超出 `effective_target_files` + ledger 扩范围记录的并集 |
| `LEDGER_FORGED` | 台账与 commit 时序明显矛盾（如一次性补齐的虚假历史）|
| `LEDGER_MISMATCH` | `effective_scope`、`ledger`、`event_log` 三者不一致 |
| `BRANCH_CONFLICT` | 分支锁显示被另一执行端持有，但当前提交者非锁持有者 |
| `TASK_OVERLAP_UNRESOLVED` | `active_tasks_index` 显示当前任务与另一活跃任务存在未解决的文件重叠（应为 paused 但实际提交了变更）|
| `SECURITY_BLOCK` | 命中安全模式检测（见第五节）|
| `TEST_BYPASS` | 检测到测试绕过（见第六节）|

---

## 五、安全模式检测

以下规则命中任一 → `[SECURITY-BLOCK]`：

| 规则 | 检测内容 |
|:---|:---|
| SEC-001 | `eval(` / `exec(` 出现在非测试文件中 |
| SEC-002 | SQL 字符串拼接 |
| SEC-003 | `subprocess` 配合 `shell=True` 且参数含外部变量 |
| SEC-004 | `yaml.load(` 无 `Loader` 参数 |
| SEC-005 | `pickle.loads(` 接收外部输入 |
| SEC-006 | 硬编码凭据 |
| PAR-001 | diff 中包含对端专属 runtime_state 或受保护文件的非授权写入 |
| PAR-002 | PR 提交历史或 commit message 中包含破坏性 git 操作 |

---

## 六、测试绕过检测

命中以下任一 → `[REVIEW-VERDICT: DENY TEST_BYPASS]`：

- `pytest.mark.skip` 无合理注释
- 注释掉的 `assert`
- 空测试
- 被测逻辑前提前 `return`

---

## 七、软告警（不阻断，记入报告）

| ID | 规则 |
|:---|:---|
| `SENSITIVE_PATH` | diff 涉及敏感路径 |
| `BRANCH_PREFIX_INFO` | 分支前缀与 `executor` 不匹配（仅告警，不阻断）|
| `EXECUTOR_MISMATCH_INFO` | PR 提交者与 `agents.md.executor` 不一致（仅告警，不阻断）|

---

## 八、Antigravity Gate 前置检查

当满足以下**双条件**时，必须有 Antigravity Gate 报告：

**条件一**（路径命中）：PR diff 包含 `workflow_registry.md § G` 定义的敏感路径
**条件二**（风险或模式命中）：满足以下任一：
  - `task_mode` 为 `dual-end-integration` 或 `frontend-integration`
  - `review_mode` 为 `gate`
  - 命中安全检测（SEC-00x）
  - 涉及鉴权/权限变更

双条件均满足时：
- 必须存在 Gate 报告
- 报告中必须含 `[AG-REVIEW-PASS]`
- 否则 → `[REVIEW-VERDICT: DENY AG-REVIEW-MISSING]`

> 例外：仅修改 `.ai_workflow/agents_history/*`、`event_log_*.jsonl`、流程文档的 PR，即使路径命中条件一，只要条件二不满足，不强制 Gate。

---

## 九、审查输出格式

### 9.1 通过

```
[REVIEW-VERDICT: PASS]
executor: <cc/cx/human>
task_id: <id>
execution_mode: <contract | ad-hoc>
completion_mode: <local-main | local-merge | PR-merge>
有效范围：✓
台账一致性：✓
安全检测：✓
```

### 9.2 不通过

```
[REVIEW-VERDICT: DENY <原因码>]
executor: <cc/cx/human>
task_id: <id>
问题列表：
1. [<规则>] <文件:行号> <问题描述>
```

---

## 十、审查执行器实现规范

任何审查执行器（Codex、Claude、人工等）均须：

1. 实现本合同第四至第八节定义的全部检查
2. 使用统一信号格式（`[REVIEW-VERDICT: PASS]` / `[REVIEW-VERDICT: DENY <码>]`）
3. 在审查报告中列出所有命中的阻断和告警
4. 不做代码风格评审，不给功能建议（仅做合规审查）

各执行器可额外定义工具特有的操作约束（如"禁止执行 git 操作"），但这些约束属于 adapter 层，不属于本合同。
