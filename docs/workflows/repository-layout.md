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
| `.claude/` | Claude Code 边界说明、repo-local guard 和本机计划草稿 |
| `.codex/` | 项目级 Codex 配置占位，不放运行态 |
| `.state/workflow/` | 旧 `cx-exec` 工作流遗留运行态，默认不提交 |
| `.state/cc-work/` | CC 计划、协作、交接、审查草稿区 |
| `scripts/` | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除 |
| `scripts/git/` | legacy/manual Git 辅助脚本 |
| `run/` | Hextech 业务运行区 |

独立项目目录 `heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/` 不属于本轮仓库整理移动范围。

## Runtime State

`.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。`.state/cc-work/**` 用于 CC 计划、协作草稿、交接稿、审查草稿和 Guard 状态文件，不是正式文档区，普通任务不强制生成文件。Guard 状态文件优先路径为 `.state/cc-work/cc-cx-state.json`。`.claude/plans/**` 只用于 Claude Code 原生运行时本机计划草稿；计划草稿写入不需要额外授权或特殊条件，默认不提交。

## Claude Guard

`.claude/settings.json` 注册 Claude Code `PreToolUse` guard，`.claude/hooks/**` 存放 guard 脚本。当前 Guard v4 负责 CX-first、CX degraded 和 CC break-glass：NORMAL 下 protected path 的探查、执行、修改和最小验证都必须委派给 Codex；`CC_BG_READ` 放行本会话只读工具；`CC_BG_WRITE` 只放行 approved plan 的 `approved_files`。未授权 `git add`、`git commit`、`git push` 直接拒绝。

## Retired Paths

以下路径只作历史警示，不得按当前结构重建：

- `.workflow/`
- `.codex-exec-apple/`
- `.learnings/`
- `run/workflow/`
- 根 `cx-exec.ps1`
- `scripts/workflow/`
- 根目录 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`.task-worktree.json`
