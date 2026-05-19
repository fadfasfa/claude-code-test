# Claude Code 本地接口

本目录只服务 Claude Code 本机运行时。当前仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md` 和 `docs/index.md` 为入口。

- `settings.json`：Claude Code 项目权限和本机 plugin 可用状态；不再注册仓库级 PreToolUse 编排 hook。
- `plans/`：Claude Code 原生运行时的本机计划草稿目录；除 `README.md` 外默认不提交。
- `skills/`：Claude Code 专用的最小辅助 skill，不属于 Codex skill 白名单。
- `worktrees/`：本地占位目录；不自动创建或主控 Git worktree。

Claude Code 可以独立完成探查、修改、验证和提交；Codex plugin 可以作为可选辅助工具，但不是主流程要求。

不要在这里保存 token、cookie、API key、proxy secret、Codex 运行态、长期报告或仓库级 workflow 规则。
