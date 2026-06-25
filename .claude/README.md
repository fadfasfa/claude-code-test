# Claude Code 本地接口

本目录只服务 Claude Code 本机运行时。当前仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md`、`docs/index.md` 和 `docs/当前规则/40-Agent与Skill.md` 为入口；业务工作区、Git 高危操作与验证收口分别以 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 为准。

- `settings.json`：Claude Code 项目权限和本机 plugin 可用状态；不再注册仓库级 PreToolUse 编排 hook，也不把 Codex plugin 作为默认执行偏好。
- 输出语言：默认使用简体中文输出计划、进展、风险、验证、审查和总结；生成计划文档、治理文档或任务总结时正文必须为简体中文，英文计划视为不合格；命令、路径、API、错误原文和分支名保持原文。
- `commands/`：Claude Code 项目级 slash command；`cleanup-worktrees` / `review` 转发到同名 skill；`review` 无参数且只有一个开放 PR 时直接审查，`all` 审查全部开放 PR；`review-all` 是可见别名，等价于 `/review all`。
- `plans/`：Claude Code 原生运行时的本机计划草稿目录；除 `README.md` 外默认不提交。
- `skills/`：Claude Code 专用的最小辅助 skill，不属于 Codex skill 白名单；`cleanup-worktrees` 只在显性调用时清理已合并、无领先提交、无 tracked 修改、无 `??` untracked 的受管或仓库内临时 review 残留；普通 ignored runtime/cache/log/data 只报告、不阻断 stale worktree 整体移除，凭据类 ignored 文件仍阻断；`review` 只做 PR 只读审查。
- `worktrees/`：本地临时 review worktree 占位目录；不作为长期主控 Git worktree，`cleanup-worktrees` 仅清理已合并且硬干净的 `agent-*` 残留。

Claude Code 可以独立完成探查、修改、验证和提交；没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 Codex / CX。

不要在这里保存 token、cookie、API key、proxy secret、Codex 运行态、长期报告或仓库级 workflow 规则。
