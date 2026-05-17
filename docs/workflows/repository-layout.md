# Repository Layout

根目录只保留入口、治理文件、工具骨架和独立工作区。普通任务只产出目标 diff 和对话摘要。

## Current Layout

| 路径 | 职责 |
| :--- | :--- |
| `AGENTS.md` / `PROJECT.md` / `README.md` / `CLAUDE.md` | 仓库入口 |
| `docs/workflows/` | 当前 workflow 规则 |
| `docs/reference/` | 按需读取的参考资料 |
| `docs/archive/` | 历史报告和退役资料，默认不读 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |
| `.claude/` | Claude Code 边界说明和 repo-local Delegation Guard |
| `.codex/` | 项目级 Codex 配置占位，不放运行态 |
| `.state/workflow/` | CC -> CX 运行态，默认不提交 |
| `scripts/workflow/` | 当前 workflow 脚本 |
| `scripts/git/` | legacy/manual Git 辅助脚本 |
| `run/` | Hextech 业务运行区 |

独立项目目录 `heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/` 不属于本轮仓库整理移动范围。

## Runtime State

CC -> CX 机器结果固定写入 `.state/workflow/tasks/<task_id>/`。`.state/workflow/reports/` 只用于审查、验收、事故复盘或 commit 前人工复核。

## Claude Guard

`.claude/settings.json` 注册 Claude Code `PreToolUse` guard，`.claude/hooks/**` 存放 guard 脚本。guard 生效后，业务工作区、workflow 控制面、仓库入口文件、`.claude/settings.json`、`.claude/hooks/**` 和 `.agents/skills/**` 默认需要通过 CC -> CX 委派修改，除非用户显性授权 CC 直接修改。

## Retired Paths

以下路径只作历史警示，不得按当前结构重建：

- `.workflow/`
- `.codex-exec-apple/`
- `.learnings/`
- `run/workflow/`
- 根目录 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`.task-worktree.json`
