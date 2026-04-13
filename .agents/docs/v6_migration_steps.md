# Hextech V6.2 → V7.1-lite 迁移要点

> 本文件仅供历史迁移 / 审计追溯，不代表当前 canonical 工作流。
> 当前主流程以 `.agents/` 下的 7.1-lite 文档为准。

## 一、主轴切换

旧设计（v6.x）：
- 执行端身份强约束 + 旧式收尾节点 + 分支锁 / 活跃任务索引
- `agents.md` 被当作日常主上下文
- 检索任务可选回写任务台账

新设计（v7.1-lite）：
- 执行身份用于追溯，不用于最终审查准入
- `agents.md` 降级为兼容性任务摘要
- small 任务默认本地完成，large 任务走 Plan Draft → 验证 → 分支实现 → PR → Codex 审查
- 检索任务完全独立，不再回写 `agents.md`

## 二、退役项

| 旧机制 | 处理方式 |
|:---|:---|
| `branch_lock` | 从主工作流中退役，不再作为执行前置条件 |
| `active_tasks_index` | 从主工作流中退役，不再作为审查核心输入 |
| `post-merge-sync` | 从主收口链路退役 |
| `finish-task` 作为全局强依赖 | 降级为本地收口动作的历史术语 |
| `retrieval_ledger_entry` | 从检索工作流中移除 |

## 三、审查输入收口

审查默认只看：
- diff
- 任务上下文摘要
- 必要的 event_log / 额外证据

不再把 `agents.md` / `execution_ledger` / `branch_lock_state` / `active_tasks_index` 当作核心必需输入。

## 四、Antigravity 定位

- 高难前端执行端
- 前端专项审查
- 周期性大型审计

普通后端执行和日常 small review 不再是它的主职责。

## 五、迁移后的主路径

| 场景 | 主路径 |
|:---|:---|
| small | `ad-hoc` + `on-demand` + `local-main` |
| large | `Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查` |
| retrieval | 独立检索链路，不进代码任务主流程 |

## 六、兼容性说明

- `PROJECT.md` 继续作为项目级总览
- `agents.md` 仅保留兼容性摘要
- 旧版文件可作为历史参考，不作为本轮执行约束
