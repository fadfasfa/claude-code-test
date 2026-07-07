# docs Index

`docs/` 的唯一发现索引。先读本文件，再按任务读取对应短规则；不要把 `docs/参考资料/`、`docs/历史归档/`、`docs/方案草稿/` 或 `docs/方法论规格/` 整体注入上下文。

## Default Reads

| 路径 | 用途 |
| :--- | :--- |
| `docs/当前规则/00-入口总览.md` | 默认流程、中文输出和阶段门禁 |
| `docs/当前规则/10-工作区登记.md` | 工作区、写入边界和业务目录保护 |
| `docs/当前规则/20-Git与高危操作.md` | Git、高危操作、worktree、PR push 和 commit/PR 语言 |
| `docs/当前规则/30-验证与审查.md` | 验证、审查和收尾报告 |
| `docs/当前规则/40-Agent与Skill.md` | agent surface、skill inventory、中文约束和重复 skill 维护 |
| `docs/当前规则/50-目录职责.md` | 仓库目录职责和生命周期 |
| `docs/当前规则/90-退役边界.md` | 旧 CC-CX、Codex surface、出口维护和 Ultraplan 边界 |

## Task Routing

| 任务类型 | 读取文档 |
| :--- | :--- |
| 默认执行流、中文输出、长任务阶段拆分 | `docs/当前规则/00-入口总览.md` |
| 长任务阶段包和子智能体验收模板 | `docs/参考资料/策略说明/long-task-stage-gate.md` |
| 工作区选择、写入边界、保护目录 | `docs/当前规则/10-工作区登记.md` |
| Git 高危操作、删除、清理、worktree、commit、push、PR | `docs/当前规则/20-Git与高危操作.md` |
| 验证、审查、收尾报告 | `docs/当前规则/30-验证与审查.md` |
| skill inventory、agent surface、重复 skill 维护 | `docs/当前规则/40-Agent与Skill.md` |
| 仓库目录职责、路径生命周期 | `docs/当前规则/50-目录职责.md` |
| Codex surface、旧 CC-CX 退役、出口维护、Ultraplan | `docs/当前规则/90-退役边界.md` |
| 旧 Superpowers project bridge 背景 | `docs/历史归档/superpowers-project-bridge.md` |

## On Demand

- `docs/参考资料/策略说明/task-routing.md`：`S/M/L` 概念说明；只定义薄边界。
- `docs/参考资料/策略说明/long-task-stage-gate.md`：长任务 / L 级任务的阶段包和子智能体 reviewer prompt 模板。
- `docs/参考资料/`：按需读取的参考资料。
- `docs/历史归档/`：历史报告和退役资料，包含已退役的 cc-worker 材料；默认不作为当前规则来源。
- `docs/方案草稿/`：设计草稿，不作为默认事实源。
- `docs/方法论规格/`：方法论规格和计划，不作为默认事实源。

## Output Boundary

普通任务不默认生成 `docs/方案草稿/*.md`、Markdown report、probe 或 archive 证据文件。
旧 `.state/workflow/**` 只作为 CC-CX 工作流遗留运行态；当前工作流不依赖旧任务结果文件。
