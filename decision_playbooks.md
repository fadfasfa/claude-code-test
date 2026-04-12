# 决策层剧本库 — Hextech
> version: 6.1

---

## A — 命令型剧本

### A-1 /AUDIT-ON

收到 `[SECURITY-BLOCK]` 后输出修订版任务卡。

### A-2 /REVIEW-ON

收到审查失败或 `[REVIEW-VERDICT: DENY ...]` 后输出纠错任务卡。

### A-3 /RECOVER

恢复时不再把 `agents.md` 视为一次性契约，而是重置为待机壳。

### A-4 /SUMMARY

接力文档必须说明：
- 当前 `execution_mode`
- 当前 `branch_policy`
- 当前 `completion_mode`
- 当前有效范围
- 运行台账是否已同步
- 分支锁状态

---

## B — 场景型剧本

### B-0.1 REQUIREMENT_REFINE SOP（需求精炼剧本）

**触发条件**：
- 用户描述仍存在多义性
- 存在多个成功标准
- 目标执行端不明确
- 是否需要数据契约/前端/后端/安全修复不明确
- 用户明确要求"尽可能精确我的需求"

**处理顺序**：
1. 用一句话重述当前理解的问题
2. 拆出尚未明确的关键维度
3. 一次性提出 3–7 个高价值澄清问题
4. 若用户回答不足，再继续第二轮澄清
5. 直到达到"可被外部 AI 脱离上下文独立验证"的精度后，才进入 Plan Draft 或验证稿阶段

**问题维度建议**：
- 你要解决的最终问题是什么
- 你接受哪些实现路径，不接受哪些
- 你要静态文案、基础公式、还是最终绝对数值
- 你要最小改动、还是允许中等重构
- 你要先验证方案，还是直接落地
- 你希望最终贴给 Codex / Claude Code / Antigravity 哪一端
- 你是否要求其他 AI 搜索网络成熟案例

### B-0.2 CROSS_AI_VALIDATION SOP（正式契约前交叉验证）

**触发条件**：
- 用户明确要求 GPT / Claude / Codex 交叉验证
- 任务存在多种实现路径
- 涉及安全 / 解析 / 公式 / 架构 / 兼容性

**输出要求**：
1. 先输出一份 Validation Draft（验证稿），以 Plan Draft 为输入底稿
2. 验证稿必须可独立转发，不依赖前文上下文
3. 验证稿必须包含：背景摘要、当前已知事实、未知项、目标结果、候选实现途径、约束条件、希望对方验证的问题、要求对方搜索的成熟案例范围、回答格式
4. 收到外部 AI 结果后，DL-Gemini 只做"收敛与比较"，不直接把任一外部 AI 结论当最终真理
5. 交叉验证完成后，才允许正式签发契约

### B-0.3 VALIDATION_DRAFT RULES

Validation Draft 必须满足：
- 不是任务卡
- 不包含 yaml 契约
- 不包含代码 / 伪代码 / diff / 命令
- 允许列出 2–4 条候选实现路径
- 每条路径必须写：可行性、风险、依赖、成熟度、是否推荐
- 必须要求对方按固定结构返回，不允许自由散文式答复

### B-0.4 LARGE_TASK_PLAN SOP（大任务规划剧本）

**触发条件**：
- `task_scale = large`（涉及文件 ≥4，或有跨模块影响，或有架构/接口变更）
- 或用户明确要求"先出计划"

**处理顺序**：

**步骤 1：读取项目文档**
- 读取 `PROJECT.md`（项目总览、文件职责清单、已知债务）
- 读取仓库中的关键规则文件（`.agents/` 目录下的协议文件）
- 通过 GitNexus 获取本地代码现状（目标文件的当前内容摘要）

**步骤 2：形成 Plan Draft**
输出 `[PLAN-DRAFT: READY]` 并包含以下六块内容：
- 背景与目标
- 当前代码现状（已读取的关键文件及职责）
- 涉及文件/模块（文件级表格：文件、当前职责、本次改动方向、不改范围）
- 建议改法与不改范围
- 风险点与回滚点
- 交叉验证问题清单

**步骤 3：发给 GPT / Claude 交叉验证**
- 以 Plan Draft 为底稿，生成 Validation Draft（见 B-0.3）
- 转发给 GPT / Claude，等待结构化验证结果

**步骤 4：收敛验证意见**
- 收到外部验证结果后，DL-Gemini 汇总共识与分歧
- 若存在阻断性分歧，输出 `[VALIDATION-BLOCKED]` + 说明
- 若达到共识，进入步骤 5

**步骤 5：交给 Codex plan mode 定稿**
- 将收敛后的 Plan Draft + 验证结论 交给 Codex
- Codex 在 plan mode 中生成最终实施计划
- 用户确认后，DL-Gemini 签发正式任务卡

**步骤 6：签发正式任务卡**
- `execution_mode: contract`
- `branch_policy: required`
- `completion_mode: PR-merge`（默认；无远端条件时降级为 local-merge）
- `dispatch.target_endpoint: cx`（当前；后期可改为 cc）

**关键限制**：
- Plan Draft 和 Validation Draft 均不包含代码
- 决策端在此阶段只输出 What/Scope/风险，不输出 How
- 不得跳过 Plan Draft 直接进入 Validation Draft 或正式契约

---

### B-1 AD-HOC-CODE SOP

**触发条件**：普通代码需求，无正式契约，但仍需长期可追踪任务总表。

1. 在 `agents.md` 写入轻量任务卡：
   - `execution_mode: ad-hoc`
   - `branch_policy: on-demand`
   - `status: active`
   - `completion_mode: local-main`（默认）
2. 默认不新建分支，并在内部状态记为 deferred。
3. **仅当用户主动发出以下明确指令之一时**，才升格为建分支 / review path：
   - 用户明确说"开分支"、"建分支"
   - 用户明确说"提交 PR"、"push"、"发起评审"
   - > ⚠️ 注意：即使任务涉及认证 / 权限 / 数据库 / 公共接口 / 敏感路径，若用户未主动要求，也**不得自动升格**。执行端应以**风险提示**方式告知用户，由用户决定是否建分支。
4. 升格时同步更新（B 类受控字段，需台账关联）：
   - `branch_policy: required`
   - `branch_name`
   - `current_review_path`
   - `review_mode: final-review` 或 `gate`
   - `completion_mode: PR-merge` 或 `local-merge`
   - `execution_ledger`（追加 `ESCALATION` + `BRANCH_CREATED` 条目）

### B-1.1 未确认态 SOP（握手锁未释放）

**触发条件**：用户尚未明确确认；或仍存在关键依赖未确认。（仅适用于 large 任务）

允许输出：
1. 需求重述（仅 What / Scope）
2. 缺失依赖列表
3. 一组关键澄清问题（1–7 个）
4. 转交其他节点的核查指令
5. Validation Draft（仅当用户要求交叉验证或满足 B-0.2 触发条件）

禁止输出：
- 任务卡字段
- `[HANDOFF 核查表]`
- `[DL-VALIDATION-PASS]`
- 任何任务卡代码块
- 实现方案、函数结构、DOM/JS/CSS 设计
- 可运行代码

### B-1.2 依赖未闭环 SOP（数据/接口契约）

**触发条件**：实现方案依赖的数据字段、接口结构、文件状态尚未被确认。

处理顺序：
1. 明确指出依赖项缺失
2. 给出最小核查指令或最小补齐方案
3. 在契约确认前，不生成依赖该字段的实现代码
4. 若用户要求继续，只允许输出"两阶段方案"：
   - 阶段 A：后端/数据补齐任务
   - 阶段 B：前端接入任务

### B-1.3 静默 HANDOFF SOP

内容要求：
- handoff 成功时：默认静默通过，不打印 checklist，不打印 validation-pass，不打印 branch-deferred
- handoff 成功的外显格式只保留：
  1. 一行粘贴目标提示
  2. 单个 yaml 契约代码块
- handoff 失败时：
  - 输出 `[HANDOFF-BLOCKED]`
  - 逐条列出缺失项
  - 不得输出契约代码块

### B-1.4 FILE_CHANGE_SPEC SOP（文件级改动说明剧本）

**触发条件**：
- 目标执行端为 Claude / Claude Code
- 或用户要求"让 Claude 生成改动后的文件"
- 或大型任务中决策端需要输出改动说明供 Claude 执行

**与普通任务卡的区别**：
普通任务卡描述"做什么"（What），File Change Spec 描述"每个文件改什么"（File-level What），Claude 按此生成目标文件。

**输出格式**（输出 `[FILE-CHANGE-SPEC: READY]` 并包含以下内容）：

```markdown
# File Change Spec
task_id: <id>
target_executor: claude | claude-code

---

## 文件：<文件路径1>

### 当前职责
<一句话说明该文件现在的用途和边界>

### 本次要改什么
<具体改动方向，不写代码，不写伪代码>

### 不要改什么
<明确禁止范围，防止 Claude 自行扩需求>

### 依赖哪些新接口/字段
<说明本次改动依赖的外部接口或数据结构，或"无">

### 需要补什么注释
<说明需要增加哪类注释，或"无">

### 需要补什么测试
<说明需要新增或修改的测试，或"无">

### 验收点
<如何判断这个文件的改动是正确的>

---

## 文件：<文件路径2>
...
```

**Claude 的职责边界**：
- 按 File Change Spec 生成改动后的目标文件或 patch 草稿
- 不负责拍板，不自行扩需求
- 不改 spec 未列入的文件
- 若发现 spec 存在冲突或歧义，输出疑问，不自行决策

**Codex 的后续职责**：
- 对 Claude 生成的文件做 diff 审核
- 跑测试，做集成修补
- 确认功能完成，决定是否提交/推送

**切换到 Claude Code 时的兼容性**：
File Change Spec 格式不变，只需将执行端从"Claude 网页端"切换为"Claude Code CLI/API"，不需要重写角色定义。

---

### B-2 FINISH-TASK SOP（统一收尾节点）

**目标**：任务完成后统一归档并恢复待机。

#### 统一收尾步骤

1. 归档当前 `agents.md` 到 `.ai_workflow/agents_history/<task_id>.md`
2. 在 `execution_ledger` 追加 `TASK_FINISHED` 条目
3. 填写 `Final_Review_Record`（如有审查则填审查结果，否则填 skipped）
4. 更新 `PROJECT.md`（必须包含：最终改了什么、最终有效范围、是否扩范围、遗留债务、task_id）
5. 释放分支锁（`release_lock`，追加 `BRANCH_LOCK_RELEASED`）
6. 用待机壳覆盖 `agents.md`（含 branch_lock + 阻塞字段重置）
7. 可选删除已合并/已完成的分支
8. 更新 `.git/hextech/active_tasks_index.json` 并触发被阻塞任务恢复

#### 三种入口

| 入口 | 触发方式 | completion_mode |
|:---|:---|:---|
| 本地 main 直接完成 | 执行端完成后直接调用 finish-task | `local-main` |
| 本地分支手动合并后 | `post-merge-sync.sh` 或手动调用 finish-task | `local-merge` |
| 远端 PR merge 后（人工 merge）| 人工 merge 后手动调用 finish-task | `PR-merge` |
| 远端 PR merge 后 *(自动，已退役)* | `auto-merge.yml` 调用远端子集 | `PR-merge` |

#### 本地入口补充

1. `git checkout main`
2. `git pull origin main`（触发 `.git/hooks/post-merge` 钩子自动执行）
3. 若 post-merge 钩子未配置，手动运行 `.ai_workflow/scripts/post-merge-sync.sh`
4. 删除已完成本地分支

### B-3 双端集成 SOP

保持原有双端集成链路，审查输入改为：
- `agents.md` 当前有效范围
- `execution_ledger`
- 双端 event_log
- `branch_lock` 状态

### B-4 前端集成 SOP

进入 PR 前必须确保：
- `PROJECT.md` 已先更新
- `agents.md.review_mode = gate`
- `agents.md.current_review_path = Gate+PR`

---

## C — Remediation Cookbook

### C-1 MERGE_AND_CLEANUP

审查通过后（`[REVIEW-VERDICT: PASS]`），进入 finish-task 流程。

若走 PR 链路：

```powershell
git checkout main
git pull origin main          # 触发 post-merge 钩子自动对齐 standby
git branch -d <已合并的分支名>
# 若未配置钩子，额外执行：
# bash .ai_workflow/scripts/post-merge-sync.sh
```

若走本地完成：
- 执行端直接调用 finish-task SOP（B-2）

### C-4 REPLAY_SCOPE_LOG

补记时不止更新 event_log，还必须同步补齐 `agents.md.execution_ledger` 与当前有效范围。

### C-5 CONTRACT_SPLIT（依赖未闭环时的双阶段拆分）

适用条件：
- 前端展示依赖后端/数据字段
- 当前接口未返回所需字段
- 依赖仍处于"建议结构"或"待补齐"状态

输出格式：
- 阶段 A：数据契约补齐任务
- 阶段 B：前端接入任务（依赖阶段 A 完成）

禁止行为：
- 在阶段 A 未完成时直接输出依赖这些字段的前端实现代码
