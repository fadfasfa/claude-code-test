# docs Index

`docs/` 的默认入口。先读本文件，再按任务读取对应短规则；不要把 `docs/reference/` 或 `docs/archive/` 整体注入上下文。

## Default Reads

| 路径 | 用途 |
| :--- | :--- |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/10-cc-cx-orchestration.md` | CC/CX 契约 |
| `docs/workflows/worktree-policy.md` | worktree 策略 |
| `docs/workflows/agent-skill-inventory.md` | skill inventory |

## On Demand

- `docs/reference/`：按需读取的参考资料。
- `docs/archive/`：历史报告和退役资料，默认不作为当前规则来源。

## Output Boundary

普通任务不默认生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
旧 `.state/workflow/**` 只作为 `cx-exec` 工作流遗留运行态；新 CC-CX 主路不依赖 `.state/workflow/tasks/result.json`。
