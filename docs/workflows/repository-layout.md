# Repository Layout

根目录只保留入口、治理文件、工具骨架和独立工作区。普通 Codex 修改只产出目标 diff 和对话摘要。

## Current Layout

| 路径 | 职责 |
| :--- | :--- |
| `AGENTS.md` | Codex 当前规则和边界 |
| `CLAUDE.md` | Claude Code 项目入口和 CC 边界 |
| `PROJECT.md` | agent 仓库地图 |
| `README.md` | 人类快速入口 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/` | 当前 workflow 规则、注册表和工具基线 |
| `docs/reference/` | 按需读取的长文参考 |
| `docs/archive/` | 历史报告、旧方案和退休日志，默认不读 |
| `.agents/` | 仓库级 Codex skill 白名单和桥接说明 |
| `.claude/` | Claude Code 占位和必要接口，不是规则真相源 |
| `.codex/` | Codex 项目配置占位，不放运行态 CODEX_HOME |
| `.state/workflow/` | CC -> CX 滚动状态和机器运行态，默认不提交 |
| `scripts/workflow/` | 当前 workflow 脚本 |
| `scripts/git/` | legacy/manual Git 辅助脚本 |
| `run/` | Hextech 业务运行区，不承载仓库级 workflow 运行态 |

独立项目目录：`heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/`。不要在仓库整理任务中移动这些目录。

## Runtime State

CC -> CX 结果固定写入：

```text
.state/workflow/tasks/<task_id>/result.json
.state/workflow/tasks/<task_id>/codex.log
.state/workflow/tasks/<task_id>/codex.err.log
```

`.state/workflow/reports/` 只用于审查、验收、事故复盘或 commit 前人工复核。`docs/plans/` 和 `docs/archive/reports/` 不是普通任务默认输出位置。

## Retired Paths

以下路径只作为历史警示，不得作为当前结构重建：

- `.workflow/`
- `.codex-exec-apple/`
- `.learnings/`
- `run/workflow/`
- 根目录 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`.task-worktree.json`
