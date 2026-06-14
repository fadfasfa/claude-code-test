# docs Index

`docs/` 的默认入口。先读本文件，再按任务读取对应短规则；不要把 `docs/reference/` 或 `docs/archive/` 整体注入上下文。

## Default Reads

| 路径 | 用途 |
| :--- | :--- |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/archive/superpowers-project-bridge.md` | 旧 bridge 归档说明 |
| `docs/workflows/independent-agent-workflow.md` | Claude Code / Codex 独立工作流 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/codex-egress-maintenance.md` | 本机 Codex 出口维护说明（本地忽略，不入库） |
| `docs/workflows/07-high-risk-safety.md` | 高危操作确认规则 |
| `docs/workflows/ultraplan-adoption-note.md` | Ultraplan 后续接入说明 |
| `docs/workflows/worktree-policy.md` | worktree 策略 |
| `docs/workflows/agent-skill-inventory.md` | skill inventory |

## On Demand

- `docs/reference/policies/task-routing.md`：`S/M/L` 概念说明；只定义薄边界。
- `docs/reference/`：按需读取的参考资料。
- `docs/archive/`：历史报告和退役资料，默认不作为当前规则来源。

## Output Boundary

普通任务不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
旧 `.state/workflow/**` 只作为 CC-CX 工作流遗留运行态；当前工作流不依赖旧任务结果文件。
