# Repository Layout

根目录只保留入口、治理文件、工具骨架和独立工作区。普通任务只产出目标 diff 和对话摘要。

## Current Layout

| 路径 | 职责 |
| :--- | :--- |
| `AGENTS.md` / `PROJECT.md` / `README.md` / `CLAUDE.md` | 仓库入口 |
| `docs/workflows/` | 当前 workflow 规则 |
| `docs/reference/` | 按需读取的参考资料 |
| `docs/archive/` | 历史报告和退役资料，默认不读 |
| `.agents/skills/` | 仓库级 Codex skill 白名单；不再放 bridge 执行入口 |
| `.claude/` | Claude Code 本机设置、入口说明和本机计划草稿 |
| `.codex/` | 项目级 Codex 配置占位，不放运行态 |
| `.state/workflow/` | 旧 CC-CX 工作流遗留运行态，默认不提交 |
| `.state/cc-work/` | 本机 agent 计划、协作、交接、审查草稿区 |
| `scripts/` | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除 |
| `scripts/git/` | legacy/manual Git 辅助脚本 |
| `run/` | Hextech 业务运行区 |

独立项目目录 `heybox/`、`qm-run-demo/`、`QuantProject/`、`sm2-randomizer/`、`subtitle_extractor/` 不属于本轮仓库整理移动范围。

## Runtime State

`.state/workflow/**` 是旧 CC-CX 工作流遗留运行态目录；当前工作流不依赖旧任务结果文件。`.state/cc-work/**` 用于本机计划、协作草稿、交接稿和审查草稿，不是正式文档区，普通任务不强制生成文件。`.claude/plans/**` 只用于 Claude Code 原生运行时本机计划草稿；计划草稿写入不需要额外授权或特殊条件，默认不提交。

## Claude Code Local Settings

`.claude/settings.json` 只保留 Claude Code 项目权限、敏感文件 deny 和本机 plugin 可用状态；不再注册仓库级编排 hook。`.claude/hooks/**` 不作为当前工作流入口。

## Retired Paths

以下路径只作历史警示，不得按当前结构重建：

- `.workflow/`
- `.codex-exec-apple/`
- `.learnings/`
- `run/workflow/`
- 旧根 CC-CX 执行脚本
- `scripts/workflow/`
- `.claude/hooks/cc-delegation-guard.ps1`
- `scripts/guard/smoke-cc-delegation-guard.ps1`
- `scripts/codex/patch-openai-codex-companion.ps1`
- 根目录 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`.task-worktree.json`
