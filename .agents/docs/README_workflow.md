# Hextech Nexus — 部署手册
> version: 6.0
> 人类速查文档。

---

## 一、架构一览

```
决策层主节点（DL-Gemini）
    ↓ 形成完整 core，签发任务卡
执行层（cc / cx / human）

本地优先模式（主力模式）：
  小任务：写 agents.md → 执行 → finish-task → standby
  大任务：写 agents.md → 建分支（获取分支锁）→ 执行 → finish-task → standby

远端 PR 链路（已退役 / Future Integration）：
  写 agents.md → 建分支 → 执行 → 更新 PROJECT.md → push → PR → 审查 → 自动合并 → finish-task

检索任务：
  默认不建分支、不建 PR
  可选在 agents.md 记一条轻量记录（retrieval_ledger_entry）
```

### 关键设计原则

- **执行身份用于追溯，不用于最终审查准入**
- **最终审查只对变更结果负责，不对执行端品牌负责**
- **并发控制**：分支锁（branch_lock）防止同分支并写；文件交集规则（active_tasks_index）负责并发调度。真正的并行准入主条件是“文件是否重叠”。
- **统一收尾**：所有完工路径（本地/PR）汇入同一 finish-task 节点

---

## 二、文件说明

| 文件 | 作用 |
| :--- | :--- |
| `workflow_registry.md` | 唯一 SSOT |
| `gemini_setting_core.md` | 决策层主节点规则 |
| `agents_template.md` | 长期任务总表模板（v6 含 branch_lock / review_mode / completion_mode） |
| `decision_playbooks.md` | 命令剧本 + SOP（含统一 finish-task） |
| `hextech-workflow.md` | 执行端协议 |
| `retrieval_workflow.md` | 检索任务独立链路 |
| `final_review_contract.md` | **审查者无关的统一审查合同**（v6 新增）|
| `codex_review_adapter.md` | Codex PR 审查适配器（实现 final_review_contract）|
| `antigravity_review_contract.md` | Antigravity Gate 审查契约 |
| `auto-merge.yml` | *(已退役)* CI 自动合并 + 远端 finish-task |
| `.ai_workflow/scripts/post-merge-sync.sh` | 本地合并后 finish-task |

---

## 三、最短工作流

### 小任务（本地直接完成）

1. 人类直接给需求
2. 执行端生成 `execution_mode=ad-hoc`、`branch_policy=on-demand`、`completion_mode=local-main` 的任务卡
3. 默认不建分支，输出 `[BRANCH: DEFERRED]`
4. 执行完成后调用 finish-task（`active → done-local → archived → standby`）

### 大任务（分支）

1. 人类描述需求
2. DL-Gemini 输出 `execution_mode=contract`、`branch_policy=required` 的任务卡
3. 执行端建分支（获取 branch_lock）执行
4. 先更新 `PROJECT.md`
5. 完工方式：
   - **本地合并（默认）**：`completion_mode=local-merge`，手动 merge 后 finish-task
   - *(已退役)* **PR 审查**：`completion_mode=PR-merge`，push → PR → 审查 → 自动合并 → finish-task

### 非契约代码任务

1. 人类直接给需求
2. 执行端默认生成 `execution_mode=ad-hoc`、`branch_policy=on-demand` 的任务卡
3. 默认不建分支，输出 `[BRANCH: DEFERRED]`
4. **仅当用户主动要求**（说"开分支"/"建分支"/"提交 PR"/"push"/"发起评审"）时才升格建分支并进入 PR 链路
5. 涉及敏感路径时执行端必须以风险提示告知用户，但不得自动升格

### 检索任务

1. `task_type = retrieval`
2. 默认不建分支、不建 PR、不写 runtime_state
3. 可选在 `agents.md` 按 `retrieval_ledger_entry` schema 记轻量检索记录
4. 输出带 citation 的结果

---

## 四、统一收尾（finish-task）

所有完工路径汇入同一 finish-task 节点：

1. 归档当前 `agents.md` → `.ai_workflow/agents_history/<task_id>.md`
2. 在 `execution_ledger` 追加 `TASK_FINISHED`
3. 填写 `Final_Review_Record`
4. 更新 `PROJECT.md`
5. 释放分支锁
6. 用待机壳覆盖 `agents.md`
7. 可选删已完成分支

### 三种入口

| 入口 | 触发方式 |
|:---|:---|
| 本地 main 完成 | 执行端完成后直接调用 |
| 本地分支合并后 | `post-merge-sync.sh` 或手动调用 |
| 远端 PR merge 后 | *(已退役)* `auto-merge.yml` 自动执行 |

> 注意：远端 PR GitHub Actions 本期已停用，全面推进本地优先模式。
> 安装钩子：`cp .ai_workflow/scripts/post-merge-sync.sh .git/hooks/post-merge && chmod +x .git/hooks/post-merge`

---

## 五、PR 描述建议字段

提交 Hextech 工作流 PR 时，描述中建议包含以下字段（v6 不再是唯一阻断条件，但有助于自动化处理）：

```
task_id: <task_id>
task_type: code
execution_mode: <contract | ad-hoc>
branch_policy: <required | on-demand>
completion_mode: PR-merge
task_mode: <standard | dual-end-integration | frontend-integration>
event_log_paths: <路径列表>
```
