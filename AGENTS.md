# 仓库级稳定规则 — Hextech
> version: 7.1-lite
> 本文件只定义稳定规则、角色边界和 review 入口，不承载项目结构说明、任务态、会话态或执行态。

---

## 一、权威分工

- `PROJECT.md`：项目/工作区稳定说明，记录结构、职责、数据流、风险、技术债务与近期变更原则
- `AGENTS.md`：仓库级稳定规则、角色边界与 review 入口
- 审查主输入：`diff`、任务说明、测试结果、必要时的 `PROJECT.md` 同步证据、必要时的 Antigravity 证据

---

## 二、角色边界

- Codex：主执行端；large 任务 PR 的主审查入口
- Antigravity：高难前端执行端、前端专项审查节点、周期性大型审计角色
- Claude / Claude Code：文件生成与辅助验证接口，不作为当前主执行端
- retrieval：独立检索任务，不进入代码任务链路

---

## 三、审查基线

- 审查只看结果和证据，不围绕旧台账、旧锁态或旧索引发言
- 小型文档修正默认按本地轻量路径处理，不要求附加任务台账
- 复杂改动必须同步检查 `PROJECT.md`
- 高风险前端接入必须具备 Antigravity 证据
- 审查不做风格挑刺，只抓实质问题

---

## 四、已退出主流程的机制

- `branch_lock`
- `active_tasks_index`
- `post-merge-sync`
- `finish-task standby reset`
- 根 `agents.md`
- 重型待机壳、恢复壳和大台账式任务壳
- 新增复杂状态机
