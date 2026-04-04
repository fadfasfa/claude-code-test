# ██ 语言强制令 ██
除代码块、Shell 命令、文件路径、报错堆栈、信号常量外，
所有终端输出、日志、THOUGHT、注释内容、用户沟通必须使用简体中文。
# ████████████████

# Hextech 工作流
> version: 5.3
> 共享常量统一见 `workflow_registry.md`。
> **agents.md = 长期任务总表**。首部字段受控；当前有效范围与运行台账允许追加更新。

---

## 协议 -1 — 自助任务卡模式

**触发**：用户直接描述需求，消息不含 `[HANDOFF-WRITE]`。

```
1. 按 agents_template.md 生成任务卡草稿
2. 默认 execution_mode=ad-hoc，branch_policy=on-demand
3. 先写 agents.md，再决定是否建分支
4. 输出 [SELF-HANDOFF: PREVIEW]
5. 用户确认后执行协议 0
```

---

## 协议 0 — 写入工作范围

写入顺序：
1. 任务头
2. 初始范围
3. 当前有效范围
4. 运行台账

并发冲突检测改为读取：
- `effective_target_files`
- `effective_modified_symbols`

状态文件仍只写本端。

---

## 协议 1 — 自主规划

输出 `[THOUGHT: 自主规划] <规划摘要>`。

---

## 协议 2 — 执行

### 2.A 分支策略

- `branch_policy = required`：必须创建新分支
- `branch_policy = on-demand`：默认不创建分支，输出 `[BRANCH: DEFERRED]`；**仅当用户主动要求**时才建分支（见 decision_playbooks.md B-1）
- `branch_policy = none`：不建分支

### 2.B 运行中变更

发现扩范围或新需求时，必须同步更新：
- `effective_target_files`
- `effective_modified_symbols`
- `effective_goals`
- `execution_ledger`
- `event_log`

不再允许只写 event_log 而不更新 `agents.md`。

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

若任务已升格进入正式评审链路，还需把：
- `status` 改为 `review-ready`
- `current_review_path` 改为 `PR` 或 `Gate+PR`
- 在 `execution_ledger` 追加 `READY_FOR_REVIEW`

---

## 合并后待机

- **云端**：`auto-merge.yml` 在 PR merge 后自动归档当前任务卡，并把仓库里的 `agents.md` 重置为与 `agents_template.md` 完全同构的 standby 壳
- **本地**：在 `git pull` 后自动触发 `.git/hooks/post-merge`；若未配置钩子，手动执行 `.ai_workflow/scripts/post-merge-sync.sh`

---

## § PR-TEMPLATE

PR 描述必须补充：

```
execution_mode: <contract | ad-hoc>
branch_policy: <required | on-demand>
```

且 `agents.md.current_review_path` 必须已升格为 `PR` 或 `Gate+PR`。
