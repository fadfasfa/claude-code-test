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
| `.claude/` | Claude Code 边界说明和 repo-local 高风险 Bash guard |
| `.codex/` | 项目级 Codex 配置占位，不放运行态 |
| `.state/workflow/` | 旧 `cx-exec` 工作流遗留运行态，默认不提交 |
| `.state/cc-work/` | CC 计划、协作、交接、审查草稿区 |
| `scripts/` | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除 |
| `scripts/git/` | legacy/manual Git 辅助脚本 |
| `run/` | Hextech 业务运行区 |

独立项目目录 `heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/` 不属于本轮仓库整理移动范围。

## Runtime State

`.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。`.state/cc-work/**` 用于 CC 计划、协作草稿、交接稿和审查草稿，不是正式文档区，普通任务不强制生成文件。

## Claude Guard

`.claude/settings.json` 注册 Claude Code `PreToolUse` guard，`.claude/hooks/**` 存放 guard 脚本。当前 guard 只负责高风险 Bash 的显式确认，例如删除文件、强制清理、回退工作树、重写 Git 历史、提交和推送；普通 `Edit` / `Write` / `MultiEdit` 与普通仓库内 Bash 不再由 repo-local hook 一刀切阻断。本仓外写入或非沙箱执行继续依赖 Claude Code 自身权限确认与 sandbox 边界。

## Retired Paths

以下路径只作历史警示，不得按当前结构重建：

- `.workflow/`
- `.codex-exec-apple/`
- `.learnings/`
- `run/workflow/`
- 根 `cx-exec.ps1`
- `scripts/workflow/`
- 根目录 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`.task-worktree.json`
