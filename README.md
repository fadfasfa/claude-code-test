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
| `scripts/` | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |

## Work Areas

业务工作区包括 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/`。治理区包括 `docs/`、`scripts/`、`.agents/skills/` 和入口文件。具体写入边界以 `docs/workflows/work_area_registry.md` 为准。

## Workflow

- Codex 独立工作时直接按 `AGENTS.md` 和任务上下文执行。
- Claude Code 入口下，CC 负责理解目标、收敛计划、监督过程、审查 diff 和验收结果；CX / Codex 负责复杂探查、实现、运行验证和第二意见。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin；plugin 启用不等于 review gate 启用，review gate 默认禁用。
- 旧 `cx-exec` 主流程已移除，`.state/workflow/**` 只作为旧运行态遗留，不再作为新主路验收接口。
- `docs/reference/` 和 `docs/archive/` 默认不整体读取。
- 普通任务不生成计划、Markdown 报告、probe 或 archive 证据文件。
