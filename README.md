# claudecode

`claudecode` 是多工作区本地开发仓库。Codex 是当前唯一主流程；Claude Code 只保留占位和必要接口。

## Daily Entry

| 路径 | 用途 |
| :--- | :--- |
| `README.md` | 人类快速入口 |
| `PROJECT.md` | agent 仓库地图 |
| `AGENTS.md` | Codex 当前规则和边界 |
| `CLAUDE.md` | Claude Code 项目入口和 CC 边界 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/` | 当前 workflow 规则 |
| `scripts/workflow/` | 当前 workflow 脚本 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |

## Work Areas

业务工作区包括 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/`。写入前以 `docs/workflows/work_area_registry.md` 为准。

仓库治理区包括 `docs/`、`scripts/`、`.agents/skills/`、入口文件和 workflow 规则。仓库根目录不承载默认业务实现。

## Workflow

- Codex 独立工作：直接按 `AGENTS.md` 和任务上下文执行。
- CC 调用 Codex：从仓库根目录运行 `.\cx-exec.ps1`，机器结果写入 `.state/workflow/tasks/<task_id>/`。
- 默认不读取 `docs/reference/` 或 `docs/archive/` 下长文，除非任务点名。
- 普通修改不生成计划、Markdown 报告、probe 或 archive 证据文件。
