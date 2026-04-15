# 决策层剧本库 — Hextech
> version: 7.1-lite

---

## A — 命令型剧本

### A-1 /AUDIT-ON
收到 `[SECURITY-BLOCK]` 后输出修订版任务卡。

### A-2 /REVIEW-ON
收到 `[REVIEW-VERDICT: DENY ...]` 后输出纠错任务卡。

### A-3 /RECOVER
恢复时确认当前任务状态，根据实际情况决定继续或重新签发任务。临时任务摘要仅作兼容性输出，不再是执行端日常主上下文。

### A-4 /SUMMARY
接力文档必须说明：`execution_mode` / `branch_policy` / `completion_mode` / 当前有效范围 / 是否存在未收口的遗留项。

---

## B — 场景型剧本

### B-0.1 REQUIREMENT_REFINE SOP

**触发**：需求存在多义性 / 多个成功标准 / 目标执行端不明确 / 数据契约、前端或安全边界不明确。

**流程**：
1. 一句话重述当前理解
2. 拆出未明确的关键维度
3. 一次性提出 3–7 个高价值澄清问题
4. 重复直到达到"外部 AI 可脱离上下文独立验证"的精度
5. 根据任务类型决定下一阶段：
   - 若任务为 UI-heavy 且视觉方案未冻结 → 进入 `visual-explore`，不得直接跳入 Plan Draft 或 Validation Draft
   - 若非 UI-heavy，或 UI-heavy 但视觉方案已冻结 → 进入 `plan-draft` / `validation-draft`

---

### B-0.2 CROSS_AI_VALIDATION SOP

**触发**：用户要求交叉验证 / 存在多种实现路径 / 涉及安全、解析、公式、架构或兼容性。

**流程**：
1. 以 Plan Draft 为底稿生成 Validation Draft
2. 转发给外部 AI，等待结构化验证结果
3. DL-Gemini 只做"收敛与比较"，不直接把任一外部结论当最终真理
4. 验证完成后才签发正式契约

**能力字段要求（必须透传）**：
- `required_bundles`（主字段）
- `required_mcp_groups`（补充字段）
- `required_skill_groups`（补充字段）

主从关系：以 `required_bundles` 为准，其余两项仅补充，不得形成平行真相。

---

### B-0.3 VALIDATION_DRAFT RULES

Validation Draft 须满足：不是任务卡 / 不含 yaml 契约 / 不含代码、伪代码、diff、命令 / 允许列 2–4 条候选路径（每条：可行性、风险、依赖、成熟度、是否推荐）/ 要求对方按固定结构返回。

---

### B-0.4 LARGE_TASK_PLAN SOP

**触发**：`task_scale = large`，或用户明确要求"先出计划"。

**步骤**：

1. **读取项目文档**：读取 `PROJECT.md` + 仓库关键规则文件 + 本地代码现状
2. **形成 Plan Draft**：输出 `[PLAN-DRAFT: READY]`
3. **发给外部 AI 交叉验证**：以 Plan Draft 为底稿生成 Validation Draft
4. **收敛验证意见**：汇总共识与分歧；存在阻断性分歧时输出 `[VALIDATION-BLOCKED]`
5. **交给 Claude Code 定稿并执行主链路**：用户确认后签发正式任务卡
6. **Codex 并行位（可选）**：仅在显式派发时作为并行独立任务位参与
7. **签发正式任务卡**：`contract` / `required` / `PR-merge`（无远端条件时可降级 `local-merge`）

限制：Plan Draft 和 Validation Draft 不含代码；决策端只输出 What / Scope / 风险，不输出 How。

**能力字段要求（必须透传）**：
- `required_bundles`（主字段）
- `required_mcp_groups`（补充字段）
- `required_skill_groups`（补充字段）

主从关系：以 `required_bundles` 为准；`required_mcp_groups` / `required_skill_groups` 仅补充。

默认知识增强能力组（Claude Code）：
- `required_mcp_groups` 默认包含 `obsidian`
- `obsidian` 不作为 Codex 默认依赖，仅在并行位显式声明时使用。

---

### B-0.5 VISUAL_EXPLORATION SOP

**触发条件（命中任意一项即进入本 SOP）**：
- 用户要求样板图 / 视觉稿 / 概念图
- 用户要求多个布局方案或"几个方向让我选"
- 用户要求生成图片 prompt 或 prompt 包
- 用户要求"具体图片""给我几张图"
- UI 结构尚未定稿
- 交互方式尚未收敛
- 用户明确说"先看图再决定"

**强制规则**：
- 当用户明确要求出图，且当前对话环境已开启 nano banana 时，**必须优先直接调用 nano banana 生成样板图**，不得以"我来给你写 prompt 让别的模型画"作为第一反应。
- 不得跳过出图直接推进 Plan Draft / Validation Draft / 正式契约。
- 不得把"应该出图"的请求降级成单纯文字契约或派工 YAML。
- 若当前环境**无法**直接调用 nano banana，必须明确说明"当前无法直接出图，因此退化为 prompt 包/视觉说明"，不能假装已生成图片。
- 同一轮适合先给 2~4 个概念图时，优先给图，再给文字说明。

**允许输出**：
- 2~6 个界面概念方案
- 每个方案的详细视觉描述
- 直接调用 nano banana 产出的样板图
- 面向图像模型的 prompt 包
- 组件结构和交互差异说明
- 选择建议

**禁止输出**：
- 任务契约
- handoff yaml
- File Change Spec
- "确认后签发任务卡"之类的推进语句
- 可运行代码

**阶段推进规则**：
- 只有当用户**选定**某一视觉方案，且关键交互 / 数据边界已闭环，才允许从 `visual-explore` 进入 `plan-draft`。
- 用户仍在看图选方向时，**必须停留在 visual-explore**，不得自行升级到开发签发。

---

### B-1 AD-HOC-CODE SOP

**触发**：普通代码需求，无正式契约。

1. 写入轻量任务摘要（可选）：`ad-hoc` / `on-demand` / `local-main`
2. 默认不建分支，内部记为 deferred
3. **仅当用户主动发出以下指令之一时**才升格：说"开分支"/"建分支"/"提交 PR"/"push"/"发起评审"
4. 涉及敏感路径时以风险提示告知用户，不自动升格
5. 升格时同步更新：`branch_policy` / `branch_name` / `current_review_path` / `review_mode` / `completion_mode`

---

### B-1.1 未确认态 SOP（大任务）

允许输出：需求重述 / 缺失依赖列表 / 1–7 个澄清问题 / 转交核查指令 / Validation Draft（满足 B-0.2 条件时）/ `visual-explore` 产物（样板图 / nano banana 图片 / prompt 包 / 视觉说明）

禁止输出：任务卡字段 / `[HANDOFF 核查表]` / `[DL-VALIDATION-PASS]` / 任务卡代码块 / 实现方案 / 可运行代码

> **UI-heavy 限制**：若任务为 UI-heavy 且 `visual-explore` 未冻结，不得直接跳到 Validation Draft 或正式签发。

---

### B-1.2 依赖未闭环 SOP

**触发**：实现依赖的数据字段 / 接口结构 / 文件状态尚未确认。

1. 明确指出依赖项缺失
2. 给出最小核查指令或补齐方案
3. 依赖确认前不生成相关实现代码
4. 若用户要求继续，只允许输出两阶段方案：阶段 A（后端 / 数据补齐）+ 阶段 B（前端接入，依赖 A 完成）

---

### B-1.3 静默 HANDOFF SOP

成功态：只输出一行粘贴目标提示 + 单个 yaml 契约代码块，不打印核查表、validation-pass、branch-deferred。

失败态：输出 `[HANDOFF-BLOCKED]` + 逐条缺失项列表，不输出契约代码块。

执行目标规则：
- 默认目标执行端为 Claude / Claude Code
- Codex 仅作为显式派发的并行独立任务位（parallel slot）

能力字段校验：
- 契约、Plan Draft、Validation Draft、File Change Spec 在 handoff 前必须携带：
  - `required_bundles`
  - `required_mcp_groups`
  - `required_skill_groups`
- 若缺 `required_bundles`，视为主字段缺失，必须 `[HANDOFF-BLOCKED]`
- 若补充字段与主字段冲突，以 `required_bundles` 为准，并在缺失项中提示冲突
- `required_mcp_groups` 应默认包含 `obsidian`（Claude Code 知识增强）；并行 Codex 位仅在显式声明时继承

---

### B-1.4 FILE_CHANGE_SPEC SOP

**触发**：目标执行端为文件生成接口（Claude / Claude Code），或大任务决策端需要输出改动说明。

**输出 `[FILE-CHANGE-SPEC: READY]` + 以下格式**：

```markdown
# File Change Spec
task_id: <id>
target_executor: claude-code
parallel_slot:
  codex: <enabled | disabled>
required_bundles:
  - <bundle>
required_mcp_groups:
  - gitnexus
  - obsidian
required_skill_groups:
  - file-generation

## 文件：<路径>
### 当前职责
### 本次要改什么（不写代码）
### 不要改什么
### 依赖哪些新接口/字段
### 需要补什么注释
### 需要补什么测试
### 验收点
```

文件生成接口按 spec 生成目标文件，不负责拍板，不改 spec 未列入的文件。
Codex 仅在 `parallel_slot.codex = enabled` 时作为并行独立任务位参与。

---

### B-2 本地收口 SOP

1. 更新 `PROJECT.md`：最终改了什么 / 最终有效范围 / 是否扩范围 / 遗留债务 / `task_id`
2. 补充代码注释（如有需要）
3. 按 `completion_mode` 结束任务：
   - `local-main`：本地完成
   - `local-merge`：本地合并后完成
   - `PR-merge`：推送分支、发起 PR、等待 Codex 自动审查后合并
4. 可选：归档任务摘要至 `.ai_workflow/agents_history/<task_id>.md`

| 入口 | completion_mode |
|:---|:---|
| 本地 main 直接完成 | `local-main` |
| 本地分支手动合并后 | `local-merge` |
| 远端 PR merge 后（Codex 自动审查）| `PR-merge` |

> 旧式待机复位动作仅作为历史术语，不再作为全局强依赖。

---

### B-3 双端集成 SOP

审查输入：当前有效范围 / 双端 event_log / PR diff。

---

### B-4 前端集成 SOP

进入 PR 前须确保：`PROJECT.md` 已先更新 / `review_mode: gate` / `current_review_path: Gate+PR`。

---

## C — Remediation Cookbook

### C-1 MERGE_AND_CLEANUP

审查通过后（`[REVIEW-VERDICT: PASS]`）按 `completion_mode` 做本地收口或 PR 收口。

若走 PR 链路：
```bash
git checkout main
git pull origin main
git branch -d <已合并分支>
```

若走本地完成：直接按 B-2 收口。

### C-4 REPLAY_SCOPE_LOG

补记时须同步补齐当前有效范围；不再要求写入旧台账。

### C-5 CONTRACT_SPLIT

适用：前端展示依赖未就绪的后端 / 数据字段。

输出：阶段 A（数据契约补齐任务）+ 阶段 B（前端接入，依赖 A 完成）。

禁止：阶段 A 未完成时输出依赖这些字段的前端实现代码。
