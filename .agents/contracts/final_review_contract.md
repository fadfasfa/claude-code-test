# 最终审查合同 — Hextech
> version: 7.1-lite
> 本文件定义审查标准，不绑定任何特定审查执行器。
> 审查只看变更结果、任务说明与必要证据，不把旧台账、旧锁态或旧索引当作核心必需输入。

---

## 一、主原则

默认主执行链为 Claude / Claude Code；Codex 作为并行位（parallel slot）按需参与。
执行身份只用于追溯，不用于最终审查准入；审查只对变更结果负责，不对执行端品牌负责。

---

## 二、审查输入

| 输入 | 来源 | 必须 |
|:---|:---|:---|
| `diff` | PR diff 或本地 git diff | 是 |
| `task_context_summary` | PR 描述、审查请求或任务说明摘要 | 是 |
| `test_results` | 必要测试结果、CI 结果或本地验证结论 | 是 |
| `project_notes` | `PROJECT.md`、PR 描述中的补充上下文或项目说明摘要 | 复杂改动时必需 |
| `antigravity_report` | `.ai_workflow/ag_review_<task_id>.md` | 仅在 Gate 触发时需要 |
| `AGENTS.md_rules` | 仓库级稳定规则说明（兼容输入；如存在则读取） | 否 |
| `capability_contract` | 三字段能力合同：`required_bundles` / `required_mcp_groups` / `required_skill_groups` | 建议（缺失时告警） |
| `parallel_slot_context` | 并行位声明与角色（主执行端/并行位）一致性信息 | 建议（适用时） |

`task_context_summary` 建议至少包含：
- `task_id`
- `task_type`
- `execution_mode`
- `branch_policy`
- `completion_mode`
- `task_mode`
- `effective_scope_summary`
- `required_bundles`
- `required_mcp_groups`
- `required_skill_groups`
- `parallel_slot`（如适用）

`project_notes` 的最低要求：
- 小改动可以只给出简要说明
- 复杂改动必须说明 `PROJECT.md` 是否已同步更新
- 若复杂改动未改 `PROJECT.md`，必须在说明里给出明确理由

读取顺序：
1. `diff`
2. `task_context_summary`
3. `test_results`
4. `project_notes`
5. `capability_contract`（如有）
6. `parallel_slot_context`（如有）
7. `antigravity_report`（如有）
8. `AGENTS.md_rules`（兼容输入，如有）

---

## 三、硬阻断条件

| ID | 规则 |
|:---|:---|
| `CONTEXT_MISMATCH` | diff、PR 描述、任务说明之间出现明显冲突 |
| `SCOPE_VIOLATION` | diff 文件超出任务上下文允许范围 |
| `PROJECT_SYNC_MISSING` | 复杂改动未同步 `PROJECT.md`，且没有给出明确豁免理由 |
| `SECURITY_BLOCK` | 命中安全模式检测（见第四节） |
| `TEST_BYPASS` | 检测到测试绕过（见第五节） |

复杂改动通常满足以下任一条件：
- diff 涉及 4 个及以上文件
- diff 跨越 2 个及以上目录
- diff 涉及 `.agents/**`、`.claude/**`、`.git/hooks/**`、`PROJECT.md`、审查合同或适配层

命中任一 → `[REVIEW-VERDICT: DENY <原因码>]`

---

## 四、安全模式检测

命中任一 → `[SECURITY-BLOCK]`：

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

## 五、测试绕过检测

命中以下任一 → `[REVIEW-VERDICT: DENY TEST_BYPASS]`：

- `pytest.mark.skip` 无合理注释
- 注释掉的 `assert`
- 空测试
- 被测逻辑前提前 `return`

---

## 六、软告警（不阻断，warning-first）

| ID | 规则 |
|:---|:---|
| `SENSITIVE_PATH` | diff 涉及敏感路径 |
| `BRANCH_PREFIX_INFO` | 分支前缀与 executor 不匹配 |
| `EXECUTOR_MISMATCH_INFO` | PR 提交者与任务说明中的 executor 不一致 |
| `DOC_MISSING` | 复杂改动未补注释或文档 |
| `CAPABILITY_CONTRACT_MISSING_INFO` | 缺失三字段能力合同，无法完整追溯能力声明 |
| `CAPABILITY_CONTRACT_INCONSISTENCY_INFO` | `required_mcp_groups`/`required_skill_groups` 与 `required_bundles` 出现不一致 |
| `PARALLEL_SLOT_MISSING_INFO` | 声明并行链路但缺失 `parallel_slot` 信息 |
| `PARALLEL_SLOT_INCONSISTENCY_INFO` | 主执行链与并行位角色声明不一致 |

说明：
- 采用 warning-first：能力合同与并行位一致性问题默认先告警，不自动升级为阻断。
- 安全、测试绕过、越权、伪造相关硬阻断规则不受本节影响。

---

## 七、Antigravity Gate 前置检查

双条件均满足时，必须有 Antigravity Gate 报告：

**条件一（路径命中）**：PR diff 包含 `workflow_registry.md § G` 定义的敏感路径

**条件二（风险或模式命中）**：满足以下任一：
- `task_mode` 为 `dual-end-integration`
- `task_mode` 为 `frontend-integration` 且命中重要前端场景（见 `antigravity_review_contract.md`）
- `review_mode` 为 `gate`
- 命中安全检测（SEC-00x）
- 涉及鉴权/权限变更

当 `task_mode = frontend-integration` 且命中重要前端场景时，`antigravity_report` 必须存在且含 `[AG-REVIEW-PASS]`。

双条件均满足时：必须存在 Gate 报告且含 `[AG-REVIEW-PASS]`，否则 → `[REVIEW-VERDICT: DENY AG-REVIEW-MISSING]`

例外：仅修改 `.ai_workflow/agents_history/*`、`event_log_*.jsonl`、流程文档的 PR，即使路径命中条件一，只要条件二不满足，不强制 Gate。

---

## 八、审查输出格式

### 8.1 通过

```
[REVIEW-VERDICT: PASS]
executor: <cc/cx/human>
task_id: <id>
execution_mode: <contract | ad-hoc>
completion_mode: <local-main | local-merge | PR-merge>
有效范围：✓
上下文一致性：✓
安全检测：✓
```

### 8.2 不通过

```
[REVIEW-VERDICT: DENY <原因码>]
executor: <cc/cx/human>
task_id: <id>
问题列表：
1. [<规则>] <文件:行号> <问题描述>
```

---

## 九、实现规范

任何审查执行器均须：

1. 实现本合同第三至第七节定义的全部检查
2. 使用统一信号格式
3. 在报告中列出所有命中的阻断和告警
4. 不做代码风格评审，不给功能建议
5. 对三字段能力合同执行一致性检查：
   - `required_bundles` 为主字段
   - `required_mcp_groups` / `required_skill_groups` 为补充字段
   - 补充字段与主字段不一致时默认输出 warning-first 告警
6. 对 `parallel_slot` 执行一致性检查：
   - 主执行链（Claude / Claude Code）与 Codex 并行位角色声明需可互相印证
   - 缺失或不一致时默认告警；若同时触发安全/越权类规则，仍按硬阻断执行

各执行器可额外定义工具特有的操作约束（adapter 层），不属于本合同。
