# ██ 语言强制令 ██
除代码块、Shell 命令、文件路径、报错堆栈、信号常量外，
所有终端输出、日志、THOUGHT、注释内容、用户沟通必须使用简体中文。
# ████████████████

# Hextech 工作流
> version: 6.0
> 共享常量统一见 `workflow_registry.md`。
> **agents.md = 长期任务总表**。首部字段受控（A 类不可变 / B 类可受控升级，见 `final_review_contract.md`）；当前有效范围与运行台账允许追加更新。

---

## 协议 -1 — 自助任务卡模式

**触发**：用户直接描述需求，消息不含 `[HANDOFF-WRITE]`。

```
1. 按 agents_template.md 生成任务卡草稿
2. 默认 execution_mode=ad-hoc，branch_policy=on-demand，completion_mode=local-main
3. 先写 agents.md，再决定是否建分支
4. 输出 [SELF-HANDOFF: PREVIEW]
5. 用户确认后执行协议 0
```

---

## 协议 0 — 写入工作范围

### 前置步骤：文件级冲突检查（必须最先执行）

遵循 `workflow_registry.md § K` 的新任务启动协议：

1. 读取 `.git/hextech/active_tasks_index.json`
2. 计算新任务的 `effective_target_files` 与所有未完成任务（即所有已在此索引中，且状态非 `archived` / `standby`，包含 `active`、`paused`、`review-ready`）的文件交集
3. 若交集非空：
   - 必须先将当前任务卡（含 `status = paused` 的状态）稳定写出到工作区并落盘
   - 设 `blocked_by_task_id`、`blocked_on_files`、`pause_reason = FILE_OVERLAP`
   - 在 `execution_ledger` 追加 `PAUSE` 条目
   - 输出 `[TASK-PAUSED: FILE_OVERLAP with <blocked_by_task_id>]`
   - 将该任务的基本信息写入 `active_tasks_index.json`（确保其中 `workspace_path` 和 `agents_md_path` 准确无误）
   - 随后中止当前执行进程，不进行后续协议，等待前序任务完成与外部唤醒
4. 若交集为空：
   - 正常落盘任务卡（`status = active`）
   - 将任务写入 `active_tasks_index.json`
   - 继续下方步骤

### 写入顺序

1. 任务头
2. 分支锁（如需建分支）
3. 审查与完工元数据
4. 任务阻塞状态
5. 初始范围
6. 当前有效范围
7. 运行台账

### 分支锁检查

若 `branch_policy = required` 或 `on-demand`+升格建分支：

1. 读取 `branch_lock.status`
2. 若 `free` → acquire_lock：
   - 设 `branch_lock.owner` = 当前端
   - 设 `branch_lock.status` = `leased`
   - 设 `branch_lock.acquired_at` = 当前时间
   - 设 `branch_lock.session_id` = 新 UUID
   - 在 `execution_ledger` 追加 `BRANCH_LOCK_ACQUIRED`
3. 若 `leased` 且 `owner` ≠ 自己 → 输出 `[BRANCH-BLOCKED: LEASED_BY_<other>]`，中止
4. 若 `leased` 且 `owner` = 自己 且 `session_id` 相同 → 允许继续（同线程重入）
5. 若 `leased` 且 `owner` = 自己 但 `session_id` 不同 → 输出 `[BRANCH-BLOCKED: SESSION_CONFLICT]`，视为同执行端不同线程冲突，进入 PAUSE

并发冲突检测以 `effective_target_files` 为主（见 `workflow_registry.md § K`）。
`effective_modified_symbols` 仅作审查参考，不作为并行准入主条件。

状态文件仍只写本端。

---

## 协议 1 — 自主规划

输出 `[THOUGHT: 自主规划] <规划摘要>`。

---

## 协议 2 — 执行

### 2.A 分支策略

- `branch_policy = required`：必须创建新分支（需先通过分支锁检查）
- `branch_policy = on-demand`：默认不创建分支，输出 `[BRANCH: DEFERRED]`；**仅当用户主动要求**时才建分支（见 decision_playbooks.md B-1）
- `branch_policy = none`：不建分支

### 2.B 运行中变更

发现扩范围或新需求时，必须同步更新：
- `effective_target_files`
- `effective_modified_symbols`
- `effective_goals`
- `execution_ledger`
- `event_log`
- `.git/hextech/active_tasks_index.json` 中自己的 `effective_target_files`

不再允许只写 event_log 而不更新 `agents.md`。

### 2.C 扩范围冲突重检

更新 `effective_target_files` 后，还须：
1. 重新检查是否与其他未完成任务（非 archived / standby）产生新的文件交集
2. 若产生新冲突且对方创建早于自己：
   - 必须自动转为 PAUSE（更新 `status=paused`，写入 `blocked_by_task_id` 等）
   - 在 `execution_ledger` 追加 `PAUSE` 条目
   - 输出 `[TASK-PAUSED: RUNTIME_OVERLAP with <task_id>]`，中止执行，等待前序任务完成

---

## 协议 3 — 自检与人工验收

收到"验收通过"后，严格顺序：

1. 先执行协议 4 更新 `PROJECT.md`
2. 再提交 docs/status 变更
3. 若已进入正式评审链路，再 push

---

## 协议 4 — 完工记录

更新 `PROJECT.md` 时必须至少写明：
- 这次最终改了什么
- 最终有效范围是什么
- 是否有中途新增需求 / 扩范围
- 当前还留了哪些债务
- 本次任务对应的 `task_id`

### 若走 PR 链路（已退役 / Future Integration）

> PR 自动合并与审查当前已退役，现仅保留本地合并（local-merge）及主分支直推（local-main）。如未来需要恢复基于远端 PR 的审查/合并工作流，可参考以下退役步骤（原剧本）：
- `status` 改为 `review-ready`
- `review_mode` 改为 `final-review` 或 `gate`
- `current_review_path` 改为 `PR` 或 `Gate+PR`
- 在 `execution_ledger` 追加 `READY_FOR_REVIEW`

### 若本地直接完成（completion_mode = local-main 或 local-merge）

- 直接调用 `finish-task`（见 decision_playbooks.md B-2）
- `status` 经 `done-local` → `archived` → `standby`

---

## 统一收尾（finish-task）

所有完工路径最终汇入同一 finish-task 节点（见 `decision_playbooks.md B-2`）：

1. 归档当前 agents.md
2. 在 `execution_ledger` 追加 `TASK_FINISHED`
3. 填写 `Final_Review_Record`
4. 更新 `PROJECT.md`
5. 释放分支锁
6. 用待机壳覆盖 agents.md
7. 可选删已完成分支

三条完工触发入口：
- 本地 main 完成 → 执行端本地直接调用 finish-task
- 本地分支合并后 → `post-merge-sync.sh` 自动调用 finish-task
- *(已退役)* 远端 PR merge 后 → `auto-merge.yml` 远端调用 finish-task 子集

> **远端补充说明：** 远端 `auto-merge.yml` 现已退役。目前的唯一支持是本地调用 finish-task。
> 步骤 8（更新 active_tasks_index + 根据被阻塞任务的 workspace_path 与 agents_md_path 定位到对方任务卡追加 RESUME 并触发恢复）默认在本地 finish-task 时执行。

---

## § PR-TEMPLATE

PR 描述建议补充（不再是唯一强依赖）：

```
task_id: <task_id>
execution_mode: <contract | ad-hoc>
branch_policy: <required | on-demand>
completion_mode: PR-merge
```

且 `agents.md.current_review_path` 必须已升格为 `PR` 或 `Gate+PR`。
