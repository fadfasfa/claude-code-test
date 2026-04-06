# 决策层剧本库 — Hextech
> version: 6.0

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

### B-1 AD-HOC-CODE SOP

**触发条件**：普通代码需求，无正式契约，但仍需长期可追踪任务总表。

1. 在 `agents.md` 写入轻量任务卡：
   - `execution_mode: ad-hoc`
   - `branch_policy: on-demand`
   - `status: active`
   - `completion_mode: local-main`（默认）
2. 默认不新建分支，输出 `[BRANCH: DEFERRED]`
3. **仅当用户主动发出以下明确指令之一时**，才升格为建分支 / review path：
   - 用户明确说"开分支"、"建分支"
   - 用户明确说"提交 PR"、"push"、"发起评审"
   - > ⚠️ 注意：即使任务涉及认证 / 权限 / 数据库 / 公共接口 / 敏感路径，若用户未主动要求，也**不得自动升格**。
   - 执行端应在任务完成前以**风险提示**方式告知用户，由用户决定是否建分支。
4. 升格时同步更新（B 类受控字段，需台账关联）：
   - `branch_policy: required`
   - `branch_name`
   - `current_review_path`
   - `review_mode: final-review` 或 `gate`
   - `completion_mode: PR-merge` 或 `local-merge`
   - `execution_ledger`（追加 `ESCALATION` + `BRANCH_CREATED` 条目）

### B-2 FINISH-TASK SOP（统一收尾节点）

**目标**：任务完成后统一归档并恢复待机。不再依赖特定入口（PR merge / 本地完成均走同一逻辑）。

#### 统一收尾步骤

1. 归档当前 `agents.md` 到 `.ai_workflow/agents_history/<task_id>.md`
2. 在 `execution_ledger` 追加 `TASK_FINISHED` 条目
3. 填写 `Final_Review_Record`（如有审查则填审查结果，否则填 skipped）
4. 更新 `PROJECT.md`
5. 释放分支锁（`release_lock`，追加 `BRANCH_LOCK_RELEASED`）
6. 用待机壳覆盖 `agents.md`（含 branch_lock + 阻塞字段重置）
7. 可选删除已合并/已完成的分支
8. 更新 `.git/hextech/active_tasks_index.json` 并触发被阻塞任务恢复（见 `workflow_registry.md § K`）：
   - 从索引移除当前任务
   - 扫描 `blocked_by_task_id = 当前 task_id` 的任务
   - 重新计算被阻塞任务与剩余活跃/paused任务的文件交集
   - 若不再有冲突 → 根据被阻塞任务记录的 `workspace_path` 和 `agents_md_path` 定位其任务卡，设其 `status = active`，清空阻塞字段，追加 `RESUME` 台账
   - 输出 `[TASK-RESUMED: <被恢复任务的 task_id>]`

#### 三种入口

| 入口 | 触发方式 | completion_mode |
|:---|:---|:---|
| 本地 main 直接完成 | 执行端完成后直接调用 finish-task | `local-main` |
| 本地分支手动合并后 | `post-merge-sync.sh` 或手动调用 finish-task | `local-merge` |
| 远端 PR merge 后 | *(已退役)* `auto-merge.yml` 调用远端子集 | `PR-merge` |

#### 远端入口（auto-merge.yml）补充（已退役）

*(注意：远端链路已退役)* PR merged 后由 GitHub Actions 自动执行步骤 1、6、7（归档、重置 standby 壳、删分支）。
步骤 2–5（TASK_FINISHED 台账、Final_Review_Record、PROJECT.md 更新、释放分支锁）应在 push 前由执行端本地完成。
步骤 8（索引更新 + 恢复被阻塞任务）在本地 finish-task 时执行。

#### 本地入口补充

1. `git checkout main`
2. `git pull origin main`（触发 `.git/hooks/post-merge` 钩子自动执行）
3. 若 post-merge 钩子未配置，手动运行 `.ai_workflow/scripts/post-merge-sync.sh`
4. 删除已完成本地分支

### B-3 双端集成 SOP

保持原有双端集成链路，但审查输入改为：
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
- GitHub Actions 自动合并并执行远端 finish-task
- 本地仅需：

```powershell
git checkout main
git pull origin main          # 触发 post-merge 钩子自动对齐 standby
git branch -d <已合并的分支名>
# 若未配置钩子，额外执行：
# bash .ai_workflow/scripts/post-merge-sync.sh
```

若走本地完成：
- 执行端直接调用 finish-task SOP（B-2）
- 无需 PR 流程

### C-4 REPLAY_SCOPE_LOG

补记时不止更新 event_log，还必须同步补齐 `agents.md.execution_ledger` 与当前有效范围。
