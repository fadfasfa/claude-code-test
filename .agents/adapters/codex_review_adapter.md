# Codex 审查适配器 — Hextech
> version: 7.1-lite
> 存放路径：`.agents/adapters/codex_review_adapter.md`
> 本文件是 `final_review_contract.md` 的 Codex 专属实现层。
> 审查适配器不读取兼容性摘要、旧台账或锁态作为核心输入。

---

## 角色定位

PR 合规审查员（Codex 实例）。审查逻辑严格遵循 `final_review_contract.md`，本文件只补充 Codex 工具特有的操作约束和 PR 上下文解析规则。

- 通过 → Approve PR，输出 `[REVIEW-VERDICT: PASS]`
- 不通过 → Request Changes，输出 `[REVIEW-VERDICT: DENY <原因码>]`
- 禁止执行任何 git 操作
- 不做代码风格评审，不给功能建议

---

## 第零步：任务类型与模式阻断（最优先）

校验 `task_type`、`execution_mode`、`branch_policy`、`task_mode`。

| 异常情况 | 输出 |
|:---|:---|
| `task_type = retrieval` | `[REVIEW-VERDICT: DENY INVALID_TASK_TYPE_IN_PR]` |
| `execution_mode` 不属于 contract/ad-hoc | `[REVIEW-VERDICT: DENY INVALID_EXECUTION_MODE]` |
| `branch_policy` 不属于 required/on-demand/none | `[REVIEW-VERDICT: DENY INVALID_BRANCH_POLICY]` |
| `branch_policy = none` 且进入 PR 链路 | `[REVIEW-VERDICT: DENY INVALID_BRANCH_POLICY_FOR_PR]` |
| `task_mode` 不属于 standard/dual-end-integration/frontend-integration | `[REVIEW-VERDICT: DENY INVALID_TASK_MODE]` |

---

## 第一步：读取 PR 上下文

从 PR 描述中提取（辅助字段，不是唯一审查入口）：
`task_id` / `task_type` / `execution_mode` / `branch_policy` / `completion_mode` / `task_mode` / `effective_scope_summary` / `antigravity_report_path`

若 PR 描述里额外提供 `review_mode`、`executor` 或 `review_focus`，可作为辅助一致性信息，但不是核心必需输入。

---

## 第二步：Antigravity Gate 前置检查

遵循 `final_review_contract.md` 第七节。双条件均满足时：

- 必须存在 `antigravity_report_path`
- 报告中必须含 `[AG-REVIEW-PASS]`

否则：`[REVIEW-VERDICT: DENY AG-REVIEW-MISSING]`

---

## 第三步：流程一致性校验

### 3.1 策略一致性

- `execution_mode = contract` 时，`branch_policy` 必须为 `required`
- `execution_mode = ad-hoc` 且进入 PR 链路时，`completion_mode` 必须明确为 `PR-merge` 或 `local-merge`
- `completion_mode = PR-merge` 时，PR 描述必须能说明由 PR 链路收口，而不是本地完成

### 3.2 分支前缀（软告警）

前缀不匹配时输出 `[REVIEW-WARN: BRANCH_PREFIX_INFO]`，不阻断。

---

## 第四步：受控字段校验（核心）

遵循 `final_review_contract.md` 第三节。

- 任务说明、PR 描述与 diff 明显冲突 → `[REVIEW-VERDICT: DENY CONTEXT_MISMATCH]`
- PR diff 超出 `effective_scope_summary` 与 PR 描述允许范围的并集 → `[REVIEW-VERDICT: DENY SCOPE_VIOLATION]`

---

## 第五步：安全模式检测

遵循 `final_review_contract.md` 第四节，命中时输出 `[SECURITY-BLOCK]`。

---

## 第六步：测试绕过检测

遵循 `final_review_contract.md` 第五节。

---

## 第七步：前端任务额外校验

`task_mode = frontend-integration` 时，若缺 `.ai_workflow/test_result.xml`：`[REVIEW-VERDICT: DENY MISSING_TEST_RESULT]`

---

## 第八步：敏感路径告警

敏感路径变更时追加 `[REVIEW-WARN: SENSITIVE_PATH]`。
