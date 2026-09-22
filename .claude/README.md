# Claude Code 本地接口

本目录只服务 Claude Code 本机运行时。当前仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md`、`docs/index.md` 和 `docs/当前规则/40-Agent与Skill.md` 为入口；业务工作区、Git 高危操作与验证收口分别以 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 为准。

- `settings.json`：Claude Code 项目默认保守权限和本机 plugin 可用状态；默认 `plan` + `Read`，不默认开放 `Bash` / `Write` / `Edit` / `MultiEdit`。
- `settings.plan.json`：严格 Plan 专用入口；配合 `claude --permission-mode plan --settings .claude/settings.plan.json` 使用，禁止 shell 与仓库文件写入；原生计划目录例外遵循 `CLAUDE.md` 与当前规则。Windows sandbox 不可用时也不把 `Bash` 当只读工具。
- `settings.implement.json`：显式执行入口；配合 `claude --permission-mode acceptEdits --settings .claude/settings.implement.json` 使用。只在已经退出 Plan 并确认要实现时使用。
- 输出语言：默认使用简体中文输出计划、进展、风险、验证、审查和总结；生成计划文档、治理文档或任务总结时正文必须为简体中文，英文计划视为不合格；命令、路径、API、错误原文和分支名保持原文。
- `commands/`：Claude Code 项目级 slash command；当前仅 `cleanup-worktrees` 转发到同名 skill。
- `plans/`：项目内本机草稿目录，不是用户级原生 `~/.claude/plans/`。严格 Plan 不写本目录；执行阶段的草稿仍须在任务授权范围内，除 `README.md` 外默认不提交。
- `skills/`：Claude Code 专用的最小辅助 skill，不属于 Codex skill 白名单；`cleanup-worktrees` 只在显性调用时清理经祖先关系或 GitHub squash OID 证据链确认的硬干净残留；普通 ignored 内容只报告，凭据类内容仍阻断。
- `worktrees/`：本地临时 review worktree 占位目录；不作为长期主控 Git worktree，`cleanup-worktrees` 仅清理已合并且硬干净的 `agent-*` 残留。

Claude Code 可以独立完成探查、修改、验证和提交；没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 Codex / CX。

Plan 阶段允许受控只读探查；需求基线获批后，唯一写入例外是系统分配的原生 `~/.claude/plans/*.md`，具体批准条件与入口遵循 `CLAUDE.md`、`docs/当前规则/40-Agent与Skill.md`，不得写入仓库业务文件。即使 Claude Code 显示 `plan` 权限模式，也不得依赖 `Bash` 作为只读保证；本机 Windows sandbox 不可用时，shell 重定向、`Copy-Item`、脚本和解释器都可能真实写盘。使用 GLM 或非 Anthropic first-party host 时，默认走 `settings.plan.json` 严格入口。

不要在这里保存 token、cookie、API key、proxy secret、Codex 运行态、长期报告或仓库级 workflow 规则。
