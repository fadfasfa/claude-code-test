# Agent 与 Skill

本文件是仓库 agent surface、skill inventory、中文输出规则和重复 Skill 维护边界。

## 语言规则

Codex 与 Claude Code 在本仓的输出语言遵循根 `AGENTS.md` 的统一约定，不复制独立的语言检查清单。

## 执行 surface

- Claude Code 和 Codex 都可以独立完成普通仓库任务。
- Codex standalone mode：用户直接调用 Codex 时，按 `AGENTS.md` 和用户任务独立执行；`PROJECT.md` 与 `docs/index.md` 指向的专项规则按任务选读。
- Claude Code 入口读取 `CLAUDE.md`、`PROJECT.md`、`docs/index.md`。
- 项目默认 `.claude/settings.json` 保守为 `plan` + `Read`。Anthropic first-party Plan 可使用宿主认可的受控只读探索，但不得通过重定向、解释器或脚本间接写盘。
- `~/.claude/plans/*.md` 是 Plan 阶段唯一允许的写入面：需求基线获批后写入系统分配的正式计划，修订时完整替换，任务结束后保留且默认不提交。
- GLM 或非 Anthropic first-party host 必须使用 `.claude/settings.plan.json`：`Edit(**)` 禁止当前工作目录写入，`Edit(~/.claude/plans/**)` 单独放行原生计划目录，并禁用 `Bash` / `PowerShell`。
- 严格 Plan 禁止 `Agent(general-purpose)`；只读 `Explore` / `Plan` agent 可按需使用，且不得把代理消息视为用户实施批准。
- OpenAI Codex plugin 可以保留启用状态；Claude Code 没有用户当前轮显性点名或命令时不得调用、委派、审查或触发 Codex / CX。
- plugin 启用不等于 review gate 启用，review gate 默认禁用。
- 普通仓库任务不修改全局 Claude Code、Codex、CLI、VS plugin、Codex App 或 proxy 配置。

## Codex Skill 白名单

| 名称 | 状态 | 触发场景 |
| :--- | :--- | :--- |
| `cleanup-worktrees` | keep | 显性调用 cleanup-worktrees 或审计/清理 managed worktree |
| `scrapling-web-scraping` | keep | Scrapling 接入、现有实现维护与爬虫替换评估；现行 runtime 位于 `run/src/hextech/infrastructure/transport` |

## Claude Code 项目入口

- `.claude/commands/cleanup-worktrees.md`：转发到 cleanup skill。
- `.claude/skills/` 只保留 Claude Code 专用最小 skill，不作为 Codex 白名单。
- `.claude/settings.json` 只保留 Claude Code 项目默认保守权限、敏感文件 deny 和本机 plugin 可用状态；不注册仓库级编排 hook。
- `.claude/settings.plan.json` 是严格 Plan 专用设置；`.claude/settings.implement.json` 通过 `Edit(**)` 和 `Bash(*)` 提供当前工作目录的显式实施入口。不要把需求基线或 Plan 草稿确认误当执行授权。
- VS Code 未自动显示正式计划时，使用 Claude Code 原生 `Ctrl+G` 或计划文件路径审查，不通过 shell 强行打开编辑器。

## 重复 Skill 维护合同

- `cleanup-worktrees` 的安全规则以 `docs/当前规则/20-Git与高危操作.md` 和两侧 skill 中的最小执行步骤共同约束；两侧必须同步祖先合并、GitHub squash OID 证据链和 expected-old-OID 删除边界。
- 通用需求澄清、编码纪律和自检由模型与当前规则直接完成；仓库不建立通用流程 skill、bridge、guardrail 或 completion gate。
- 不维护大段复制的双份说明；专项 skill 必须短、明确、只保存本仓特有事实。
- 修改任一侧重复 skill 时，必须检查另一侧是否需要同步。

## 禁止恢复

- 不恢复 memory、learning promotion、自动 PR shipping、高权限 worktree governance 或 task resume skill。
- 不恢复旧 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- 不恢复 Superpowers、通用 brainstorming、TDD、worktree、review、verification、planning 元流程 Skill 或其替代串联。
- `S/M/L` 只作为治理边界，不承载 Skill 加载或执行职责。
