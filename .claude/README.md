# Claude Code 本地接口

本目录只服务 Claude Code 本机运行时。当前仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md` 和 `docs/index.md` 为入口。

- `settings.json`：Claude Code 项目权限和本机 plugin 可用状态；不再注册仓库级 PreToolUse 编排 hook，也不把 Codex plugin 作为默认执行偏好。
- `commands/`：Claude Code 项目级 slash command；当前只保留用户显性调用的 `/cleanup-worktrees`。
- `plans/`：Claude Code 原生运行时的本机计划草稿目录；除 `README.md` 外默认不提交。
- `skills/`：Claude Code 专用的最小辅助 skill，不属于 Codex skill 白名单；`cleanup-worktrees` 只在显性调用时审计或按授权清理 managed worktree。
- `worktrees/`：本地占位目录；不自动创建或主控 Git worktree。

Claude Code 可以独立完成探查、修改、验证和提交；没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 Codex / CX。

不要在这里保存 token、cookie、API key、proxy secret、Codex 运行态、长期报告或仓库级 workflow 规则。
