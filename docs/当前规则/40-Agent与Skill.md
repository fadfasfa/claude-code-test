# Agent 与 Skill

本文件是仓库 agent surface、skill inventory、中文输出规则和重复 Skill 维护边界。

## 语言规则

- Codex 与 Claude Code 在本仓默认使用简体中文输出计划、进展、问题、风险、验证结果、审查结论和最终总结。
- 生成计划文档、治理文档或任务总结时，正文必须使用简体中文，除非用户明确要求其他语言。
- 用户或材料明确要求其他语言时按请求执行。
- 技术标识符、命令、路径、API、协议字段、分支名、错误原文和 Skill 名称保持原文。
- 生成英文计划视为未满足本仓维护规则，交付前必须改为中文。

## 执行 surface

- Claude Code 和 Codex 都可以独立完成普通仓库任务。
- Codex standalone mode：用户直接调用 Codex 时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务独立执行。
- Claude Code 入口读取 `CLAUDE.md`、`PROJECT.md`、`docs/index.md`。
- Claude Code Plan 阶段默认严格只读。项目默认 `.claude/settings.json` 保守为 `plan` + `Read`；严格 Plan 使用 `.claude/settings.plan.json`，显式实现使用 `.claude/settings.implement.json`。
- Plan 阶段不得把 `Bash` / `PowerShell` 当只读工具；Windows sandbox 不可用时 shell 可以通过重定向、复制、解释器或脚本真实写盘。使用 GLM 或非 Anthropic first-party host 时必须走严格 Plan 入口。
- OpenAI Codex plugin 可以保留启用状态；Claude Code 没有用户当前轮显性点名或命令时不得调用、委派、审查或触发 Codex / CX。
- plugin 启用不等于 review gate 启用，review gate 默认禁用。
- 普通仓库任务不修改全局 Claude Code、Codex、CLI、VS plugin、Codex App 或 proxy 配置。

## Codex Skill 白名单

| 名称 | 状态 | 触发场景 |
| :--- | :--- | :--- |
| `karpathy-guardrail` | keep | 架构/实现/debugging/数据管线方案防范围漂移；不承担编码实现纪律 |
| `frontend-design-project-bridge` | keep | 前端 UI / 视觉 / 交互任务 |
| `repo-verification-before-completion` | keep | 声明完成前 |
| `repo-maintenance` | keep | 仓库维护、清理候选、保护资产检查 |
| `repo-local-pr-review` | keep | commit / PR 前本地审查 |
| `repo-module-admission` | keep | 新增 workflow module、skill、hook、tool 或工作区前 |
| `cleanup-worktrees` | keep | 显性调用 cleanup-worktrees 或审计/清理 managed worktree |
| `scrapling-web-scraping` | keep | Scrapling、网页抓取、动态网页、结构化抽取或爬虫替换评估 |

## Claude Code 项目入口

- `.claude/commands/cleanup-worktrees.md`：转发到 cleanup skill。
- `.claude/commands/review.md`：转发到 review skill。
- `.claude/commands/review-all.md`：`/review all` 可见别名。
- `.claude/skills/` 只保留 Claude Code 专用最小 skill，不作为 Codex 白名单。
- `.claude/settings.json` 只保留 Claude Code 项目默认保守权限、敏感文件 deny 和本机 plugin 可用状态；不注册仓库级编排 hook。
- `.claude/settings.plan.json` 是严格 Plan 专用设置；`.claude/settings.implement.json` 是显式执行设置。不要在同一会话内把 Plan 草稿确认误当执行授权。

## 重复 Skill 维护合同

- `cleanup-worktrees` 的安全规则以 `docs/当前规则/20-Git与高危操作.md` 和两侧 skill 中的最小执行步骤共同约束。
- `karpathy-guardrail` 只约束方案范围；编码实现纪律由两端原生 `karpathy-guidelines` 提供。
- 不维护大段复制的双份说明；两侧 skill 必须短、明确、指向当前事实源。
- 修改任一侧重复 skill 时，必须检查另一侧是否需要同步。

## 禁止恢复

- 不恢复 memory、learning promotion、自动 PR shipping、高权限 worktree governance 或 task resume skill。
- 不恢复旧 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- `brainstorming` 与 `karpathy-guidelines` 由 Codex、Claude Code 各自的原生目录维护；本仓不 fork、复制或桥接这两个基线 Skill。
- `S/M/L` 只作为治理边界，不承载 Skill 加载或执行职责。
