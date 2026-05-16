# docs Index

`docs/` 的默认入口。先读本文件，再按任务读取对应短规则；不要把 `docs/reference/` 或 `docs/archive/` 整体注入上下文。

## Default Reads

| 路径 | 用途 |
| :--- | :--- |
| `docs/workflows/00-overview.md` | workflow 短总览 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/10-cc-cx-orchestration.md` | CC/CX 契约 |
| `docs/workflows/worktree-policy.md` | worktree 策略 |
| `docs/workflows/agent-skill-inventory.md` | 仓库级 Codex skill inventory |

## On Demand

- `docs/reference/`：长文政策、learning 摘要和可选参考，任务点名时读取具体文件。
- `docs/archive/`：历史报告、旧方案和退休日志，默认不作为当前规则来源。

## Output Boundary

普通 Codex 修改任务不生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
