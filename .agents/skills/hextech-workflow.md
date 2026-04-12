# Hextech 工作流
> version: 7.1-lite
> `agents.md` 仅是兼容性任务摘要，不是执行端日常主上下文。
> 共享常量统一见 `workflow_registry.md`。

---

## 协议 -1 — 自助任务卡模式

**触发**：用户直接描述需求，消息不含 `[HANDOFF-WRITE]`。

1. 按 `agents_template.md` 生成轻量任务摘要草稿
2. 默认 `execution_mode=ad-hoc`，`branch_policy=on-demand`，`completion_mode=local-main`
3. 默认不建分支
4. 用户确认后进入执行流程

---

## 协议 0 — 任务启动

### 小任务

- 直接本地完成
- 不要求分支锁
- 不要求旧任务索引
- 以最小有效范围推进

### 大任务

1. 先产出 `Plan Draft`
2. 再做 `Validation Draft`
3. 用户确认后按 `contract` / `required` 启动分支实现
4. 完成后走 PR 链路并接受 Codex 自动审查

---

## 协议 1 — 执行

### 1.A 分支策略

- `required`：必须创建新分支
- `on-demand`：默认不创建分支，只有用户主动要求才升格
- `none`：不建分支

### 1.B 运行中变更

发现扩范围或新需求时，更新：

- `effective_target_files`
- `effective_goals`
- `summary`
- `event_log`

### 1.C 主路径收口

- `small`：直接执行 → 更新 `PROJECT.md` / 注释 → 提交
- `large`：`Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查`

---

## 协议 2 — 附属链路

- `retrieval` 任务走 `retrieval_workflow.md`
- `review_mode: gate` 仅在满足 `workflow_registry.md` 的 Gate 条件时启用

---

## 协议 3 — 兼容性说明

- `agents.md` 仅保留兼容性摘要与轻量说明
- 不再把旧锁、旧索引、旧同步脚本或旧式待机复位动作作为工作流强依赖
- 本文件只定义执行骨架，不定义项目级实例文件
