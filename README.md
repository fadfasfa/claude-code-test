# claudecode

`claudecode` 是多工作区本地开发仓库。Codex 是当前唯一主流程；Claude Code 只保留入口和边界说明。

## Daily Entry

| 路径 | 用途 |
| :--- | :--- |
| `README.md` | 人类快速入口 |
| `PROJECT.md` | agent 仓库地图 |
| `AGENTS.md` | Codex 规则和边界 |
| `CLAUDE.md` | Claude Code 入口 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/` | 当前 workflow 规则 |
| `scripts/workflow/` | 当前 workflow 脚本 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |

## Work Areas

业务工作区包括 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/`。治理区包括 `docs/`、`scripts/`、`.agents/skills/` 和入口文件。具体写入边界以 `docs/workflows/work_area_registry.md` 为准。

## Workflow

- Codex 独立工作时直接按 `AGENTS.md` 和任务上下文执行。
- CC 调用 Codex 时从仓库根目录运行 `.\cx-exec.ps1`，机器结果写入 `.state/workflow/tasks/<task_id>/`。
- `docs/reference/` 和 `docs/archive/` 默认不整体读取。
- 普通任务不生成计划、Markdown 报告、probe 或 archive 证据文件。
