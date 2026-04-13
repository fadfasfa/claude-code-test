# Hextech Nexus — 部署手册
> version: 7.1-lite
> 人类速查文档。

---

## 一、架构一览

```
决策层主节点（DL-Gemini）
  小任务：伴随式指导 + 局部验收
  大任务：读 PROJECT.md → Plan Draft → Validation Draft → 签发任务卡 → 派工

执行层（Codex 主执行 / Antigravity 高难前端执行 / Claude 与 Claude Code 文件生成接口 / human 可承接）
审查层：大任务 PR 默认使用 Codex 自动审查入口
```

**核心原则**
- 执行身份用于追溯，不用于审查准入
- 审查只对变更结果负责
- 统一收口由 `completion_mode` 决定，不再依赖旧的锁式同步流程
- 文档即交付物：代码改动必须同步更新 `PROJECT.md`，复杂逻辑必须补注释

---

## 二、文件说明

| 文件 | 作用 |
| :--- | :--- |
| `workflow_registry.md` | 唯一 SSOT（常量 / 路由 / 信号） |
| `gemini_setting_core.md` | 决策层主节点规则 |
| `agents_template.md` | 兼容性任务摘要模板 |
| `decision_playbooks.md` | 命令剧本 + SOP |
| `hextech-workflow.md` | 执行端协议 |
| `retrieval_workflow.md` | 检索任务独立链路 |
| `final_review_contract.md` | 审查者无关的统一审查合同 |
| `codex_review_adapter.md` | Codex PR 审查适配器 |
| `antigravity_review_contract.md` | Antigravity Gate 审查契约 |

---

## 三、主工作流

### 小任务（本地直接完成）

1. 执行端直接在 main 上工作（`ad-hoc` / `on-demand` / `local-main`）
2. 决策端伴随指导、纠偏、局部验收
3. 完成后按 `completion_mode` 做本地收口
4. 复杂逻辑补注释，`PROJECT.md` 同步更新

### 大任务（分支 + PR，默认路径）

1. DL-Gemini 读取 `PROJECT.md` 和本地代码现状
2. 输出 `Plan Draft`，发给外部 AI 交叉验证
3. 收敛后交给 Codex plan mode 定稿
4. DL-Gemini 签发 `contract` / `required` / `PR-merge` 任务卡
5. 执行端建分支执行
6. 更新 `PROJECT.md`，补必要注释
7. `push → PR → Codex 自动审查 → merge`

### 大任务（本地合并，降级选项）

步骤 1–6 同上，第 4 步改为 `completion_mode: local-merge`。

### 非契约小代码任务

1. 执行端生成 `ad-hoc` / `on-demand` 任务摘要，默认不建分支
2. 仅当用户明确要求时才升格建分支并进入 PR 链路
3. 涉及敏感路径时以风险提示告知用户，不自动升格

### 检索任务

1. `task_type: retrieval`，不建分支、不建 PR
2. 输出带 citation 的结果
3. 不挂回 `agents.md` 的主工作流

### 文件生成接口（Claude / Claude Code）

大任务中作为验证端和文件生成接口：接收 `Plan Draft` 做交叉验证，接收 `File Change Spec` 生成目标文件草稿。不负责拍板，不改 spec 未列入的文件。Codex 负责 diff 审核和集成验收。

---

## 四、执行端硬要求

1. 代码改动必须同步更新 `PROJECT.md`
2. 以下情况必须补注释或文档：新增复杂条件分支 / 修改关键数据流 / 修改鉴权安全逻辑 / 引入临时兼容逻辑 / 修复意图不明显的问题
