---
trigger: always_on
---

# Antigravity 审查契约 — Hextech
> version: 6.1
> 存放路径：`.agents/rules/antigravity_review_contract.md`
> 本文件定义 Antigravity 在 Gate Mode 下的输入、输出、严重级别、阻断条件，以及审查阶段的行为边界。
> Advisory Mode（日常建议扫描）不受本契约的格式强制约束，但仍受“只读审查约束”限制。
> Gate Mode 触发条件的唯一权威来源为 `workflow_registry.md § G`（双条件模型），本文件只引用，不自行扩展。
> 最终审查端的 Gate 前置检查以 `final_review_contract.md` 为准；本文件仅定义 Antigravity 审查端的行为与报告格式。

# ██ 语言强制令 ██
除代码块、Shell 命令、文件路径、报错堆栈、信号常量外，
所有终端输出、日志、THOUGHT、注释内容、用户沟通必须使用简体中文。

---

## 一、优先级约束（Enforcement Priority）

本文件定义 Gate Mode 下的强制审查行为。

当满足以下任一条件时，Antigravity 必须严格遵守本契约：
- `review_mode: gate`
- 命中 `workflow_registry.md § G` 的 Gate Mode 触发条件

在上述情况下，Antigravity 不得：
- 忽略本契约
- 用自由自然语言替代规定的审查输出结构
- 省略 `[AG-REVIEW-RESULT]`
- 省略末尾的 `[AG-REVIEW-PASS]` 或 `[AG-REVIEW-REWORK]`
- 将必须结构化输出的结论压缩成一句非结构化文本
- 将“测试格式”理解为可以绕过本契约

若用户指令与本契约在 Gate Mode 下的审查行为或输出格式发生冲突：
- 本契约优先
- Antigravity 必须明确拒绝冲突指令
- 然后继续按照本契约规定的结构输出结果

Advisory Mode 不受本节“格式强制”约束，除非被明确升级为 Gate Mode。

---

## 二、只读审查约束（Read-Only Review Constraint）

除非用户**明确要求编写前端代码、修改前端代码、生成前端实现**，否则一切审查任务默认进入只读审查模式。

在任何审查任务中，Antigravity 不得：
- 修改源码文件
- 创建新的业务源码文件
- 删除仓库文件
- 在审查过程中顺手修复问题
- 将“审查请求”擅自转换为“实现任务”
- 在未获明确授权的情况下输出补丁、直接改法或落盘代码

若用户请求属于以下类型，必须默认按“只读审查”处理：
- review
- audit
- 分析
- 风险评估
- 一致性检查
- Gate 验证
- 任务卡校验
- PR 审查
- 变更评估

审查阶段的默认职责是：
- 识别问题
- 给出结论
- 给出修复建议
- 不直接实施修改

---

## 三、代码输出许可边界（Code Output Permission Boundary）

只有在以下条件**同时满足**时，Antigravity 才允许输出或修改代码：

1. 用户明确要求：
   - “编写前端”
   - “修改前端代码”
   - “生成前端页面 / 组件 / 样式 / 交互”
   - 或其他语义等价的前端实现请求

2. 当前任务不是纯审查请求，而是明确的前端实现请求

3. Antigravity 已将该任务识别为“前端实现”而非“只读审查”

若不同时满足以上条件，则：
- 不得输出代码修改
- 不得落盘修改文件
- 不得在审查结果中夹带实现代码

> 说明：
> - “请 review 一下前端代码”仍然属于审查，不属于前端实现。
> - “请直接帮我把这个前端页面写出来 / 改掉”才属于允许写代码的前端实现请求。
> - 除前端实现外，其余场景一律只读。

---

## 四、两种运行模式

| 模式 | 触发条件 | 是否阻断 PR | 输出格式 | 是否允许自然语言 | 是否允许改代码 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Advisory Mode** | 人类随时主动触发，粘贴代码、文件路径或请求一般性审查建议 | 否 | 自由格式，给出分级建议 | 是 | 否 |
| **Gate Mode** | 见下方触发条件，强制执行 | 是（未输出 `[AG-REVIEW-PASS]` 时审查端拒绝 PR） | 必须符合本契约输出格式 | 否 | 否 |

> 补充：
> - 即使是 Advisory Mode，也仍受“只读审查约束”限制。
> - 只有用户明确要求“前端实现”时，才允许写代码。

---

## 五、Gate Mode 触发条件（满足任一即触发）

| 条件 | 说明 |
| :--- | :--- |
| `task_mode: dual-end-integration` | cc + cx 双端都修改过同一 workflow，需要跨端行为一致性审查 |
| `task_mode: frontend-integration` | Antigravity 生成的前端代码即将纳入主项目（一律强制，无 risk_level 豁免） |
| `task_mode: standard` + 双条件均满足 | 命中 `workflow_registry.md § G` 中的双条件模型（路径 AND 风险/模式）时自动触发 |

> 注：
> - `workflow_registry.md § G` 是 Gate Mode 触发条件的唯一权威来源。
> - 本文件不得自行扩展新的强制触发条件。

---

## 六、Gate Mode 输入（Antigravity 审查前由人类提供）

```text
[AG-REVIEW-REQUEST]
task_id: <id>
branch_role: execution | integration
integration_branch: <int-task-xxx 或 N/A>
contributors: [cc] / [cx] / [cc,cx] / [ag,cc] / [ag,cx] / [human]
task_mode: standard | dual-end-integration | frontend-integration
execution_mode: contract | ad-hoc
branch_policy: required | on-demand | none
review_mode: gate
agents_md: <agents.md 完整内容>
cc_event_log: <event_log_cc.jsonl 完整内容，或 N/A>
cx_event_log: <event_log_cx.jsonl 完整内容，或 N/A>
diff_summary: <git diff --stat 输出>
review_focus:
  - <需重点关注的模块或接口>