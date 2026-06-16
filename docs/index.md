# docs Index

`docs/` 的唯一发现索引。先读本文件，再按任务读取对应短规则；不要把 `docs/reference/` 或 `docs/archive/` 整体注入上下文。

## Default Reads

| 路径 | 用途 |
| :--- | :--- |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/workflows/independent-agent-workflow.md` | Claude Code / Codex 独立工作流 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/07-high-risk-safety.md` | 高危操作确认规则 |

## Task Routing

| 任务类型 | 读取文档 |
| :--- | :--- |
| 工作区选择、写入边界、保护目录 | `docs/workflows/work_area_registry.md` |
| Claude Code / Codex 独立执行流 | `docs/workflows/independent-agent-workflow.md` |
| Codex surface、旧 CC-CX 退役、执行边界 | `docs/workflows/codex-execution-boundary.md` |
| Codex 出口、代理、账号池、`7898` / `7899`、路由维护 | `docs/workflows/codex-execution-boundary.md`，必要时再读本地忽略的 `docs/workflows/codex-egress-maintenance.md` |
| Git 高危操作、删除、清理、发布边界 | `docs/workflows/07-high-risk-safety.md` |
| commit message、PR 标题和正文语言格式 | `docs/workflows/git-language-policy.md` |
| worktree 创建、命名、清理、managed root | `docs/workflows/worktree-policy.md` |
| skill inventory、旧 bridge/skill 退役状态 | `docs/workflows/agent-skill-inventory.md` |
| 仓库目录职责 | `docs/workflows/repository-layout.md` |
| Ultraplan 后续接入 | `docs/workflows/ultraplan-adoption-note.md` |
| 旧 Superpowers project bridge 背景 | `docs/archive/superpowers-project-bridge.md` |

## On Demand

- `docs/reference/policies/task-routing.md`：`S/M/L` 概念说明；只定义薄边界。
- `docs/reference/`：按需读取的参考资料。
- `docs/archive/`：历史报告和退役资料，默认不作为当前规则来源。

## Output Boundary

普通任务不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
旧 `.state/workflow/**` 只作为 CC-CX 工作流遗留运行态；当前工作流不依赖旧任务结果文件。
