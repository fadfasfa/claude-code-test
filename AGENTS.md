# claudecode Agent 规则

`claudecode` 是一个多功能开发仓库。它包含多个相互独立的业务工作区，也包含本仓 repo-local 的 Claude Code 工作流规则。仓库根目录是治理与任务路由控制面，不是默认业务实现目录。

## 当前执行角色

- Claude Code 是本仓日常主力开发端，负责从工作区上下文进入、实施业务改动、运行本地验证并汇报结果。
- Codex 主要承担全局 / 跨仓库能力治理、重构辅助、个人助理型任务和云端 PR 审查；在本仓内不作为必须经过的任务派发端。
- Web 端 AI 只用于需求收敛、知识搜集和意见参考；它不下发任务，不替代本仓最终决策。
- Antigravity 只由人工显式触发，用于 Claude Opus 4.6 独立审查或 Gemini 高难前端落地；本仓不启用 Antigravity Gate。
- `.ai_workflow`、`finish-task`、`event_log`、`active_tasks_index` 属于已退役机制，不恢复、不引用、不作为执行状态机。
- `planning-with-files` 保留为 Claude Code 规划辅助 skill / plugin，不是强制任务台账，也不是危险操作授权源。
- 全局 `C:\Users\apple\.agents\skills` 已冻结，不作为正式 skill 来源；正式 Claude Code skill 来源是全局 `.claude\skills` 与本仓 `.claude\skills`。

## 规则读取链

通用 agent 或 Codex 风格会话在改代码或改工作流文件前，先读这些 repo-local 文件。Claude Code 会话从 `CLAUDE.md` 进入，再跟随本文件；两者属于同一条规则链，只是入口不同。

1. `AGENTS.md`
2. `CLAUDE.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. 相关 `docs/` 文件

入口文件不是每次都要全量读取。普通任务优先读取中文头部简介、目录、目标工作区相关段落和相关小节；已知 `target_work_area` 时，只搜索该工作区相关段落。不要默认读取 `AGENTS.md`、`PROJECT.md` 或 `work_area_registry.md` 的 1-2000 行，也不要把入口链全文件读取当作启动固定动作。

### Native Read limits for text/code files

当前本仓已知 Claude Code 在 Codex Proxy / GPT 模型路径下，原生 `Read` 对 text/code/Markdown 文件可能自动携带 `pages: ""` 或错误 `pages` 参数并失败。因此默认不把原生 `Read` 当作 Markdown/text/code 探索入口，只保留明确的 Edit 前置登记例外：

- Markdown/text/code 文件默认不要用原生 `Read` 做大范围阅读，不得传 `pages` 参数；优先用 `rg`、`Get-Content`、Python UTF-8 line-number preview、`Grep` / `Glob` / scoped `Bash` 等窄范围读取。
- 当下一步需要使用 `Edit` 修改某个 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml`、`.yml`、`.toml`、`.csv` 目标文件时，允许对该目标文件执行一次原生 `Read` 作为 `Edit` 前置登记；许可只针对即将被 `Edit` 的目标文件，不得借此读取整批 Markdown/text 文件或扩大探索范围。
- 不得给 Markdown/text/code 文件的原生 `Read` 传 PDF `pages` 参数；PDF 才允许页范围语义，Markdown/text/code 只允许路径与行范围/片段读取。
- 如果主线程或 subagent 已经发生一次 `Read` `pages` / `schema` / unsupported parameter / malformed input / `Invalid pages parameter` 失败，不得重试同文件同类 `Read`，不得声称“这次不传 pages”后再次发起同类 `Read`；上面的 Edit 前置登记例外不适用于该失败场景。
- 源码和文档探索优先使用 `.claude/agents/repo-explorer.md`，或直接用 `Grep` / `Glob` / `Bash` 获取少量片段。
- `repo-explorer` 只能使用 `Grep` / `Glob` / `Bash`。
- 需要上下文时，用 `Grep` / `Glob` / `Bash` 获取片段或让 `repo-explorer` 汇总；非 Edit 前置登记场景如果仍需要完整上下文，停止并报告 blocker。
- 在 Windows / Claude Bash 中摘要 `.learnings`、日志或源码片段时，显式使用 UTF-8/`errors="replace"` 或 ASCII-safe 字段，并限制输出范围；不要原样倾倒乱码、替换字符或长 payload。
- 如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而不可用，先按上方目标文件登记例外对即将 `Edit` 的目标文件执行一次原生 `Read`；若已出现 `pages` / `schema` / malformed / `Invalid pages parameter` 失败，则不得再用该例外，改用 scoped shell/Python 只读确认后报告 blocker 或等待授权。不得临时用 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 绕过。
- 只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才允许用脚本化补丁修改该文件；执行前必须只读确认目标字符串或块唯一匹配，输出匹配数量，匹配不是 1 就停止，不扩大替换范围或猜测邻近片段。
- `full_synergy_scraper.py` 这类源码文件不能“重试读取”；首次遇到 `Read` 参数失败后，同文件原生 `Read` 路径立即关闭。

只读探索默认使用 `.claude/agents/repo-explorer.md`。本仓不使用 built-in `Explore` 承担需要中文 Todo、原生 `Read` 禁令或路径纪律的只读探索任务；对应 `Agent(Explore)` 会被 repo-local PreToolUse hook 拦截。

本仓不继承 `kb` 工作流。只有用户明确要求做边界对比或污染风险检查时，才可只读参考 `kb`。不得从本仓工作流修改 `kb`。

本仓也不修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App、Codex Proxy、全局 hooks 或全局 skills；除非用户另起一个全局层任务。

## Codex 本仓补充

Codex 不继承 Claude Code 的全局 `CLAUDE.md`；在本仓执行时先遵守 Codex 自己的 `AGENTS.md` 链，再遵守本仓规则。

- 默认使用简体中文输出任务总结、风险说明、验证结果和变更说明。
- 重要规则、工作流、工具或代码文件应有中文功能简介或模块说明；新增或大改的 Python / PowerShell / JavaScript / HTML 代码，只在必要位置补中文注释、docstring 或 JSDoc。
- 中文注释解释“为什么”和边界条件，不重复代码表面行为；技术标识符、API 名称、库名、协议字段、命令名保持英文原文。
- 优先写简单、直接、可局部理解的代码；可读性优先于技巧性，避免 clever code。
- 小步修改，一个 patch 只解决一个清晰问题；不做顺手重构，不扩大任务范围。
- 优先显式数据流，减少隐藏状态和隐式副作用；保持函数短小、命名清楚、边界明确。
- 失败要尽早暴露，错误信息要可诊断；避免过早抽象，稳定模式重复出现后再抽象。
- 对危险操作、路径操作、删除操作、Git 操作必须先做只读检查；如果工具链异常，不要绕过安全边界继续大改，先报告 blocker 或做最小诊断。
- 如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而失败，先按 Native Read limits 中的目标文件登记例外补一次目标文件原生 `Read`；若已出现 `pages` / `schema` / malformed / `Invalid pages parameter` 失败，不得再补同类 `Read`。如果 `Edit` 仍失败，必须停止并报告具体失败原因。不得退到 PowerShell `Set-Content`、`[System.IO.File]::WriteAllText` 或 ad-hoc replacement scripts 修改业务文件。只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才能继续。
- Codex 默认不修改 `run/**`，除非用户本轮明确要求业务改动。
- Codex 不默认清理 branch / worktree，不默认运行 destructive 命令，不默认创建长期 worktree。
- Codex 总结必须列出：修改文件、是否触碰 `run/**`、是否执行删除/清理、是否提交、验证结果。

## 任务路由

详细路由规则见 `docs/task-routing.md`。

- 小任务和明确 bug 修复走轻量路径：确认目标工作区，读取窄范围文件，修改，运行最近的有效验证，报告结果。
- 中任务走简短计划和明确验证。TDD、worktree、subagent、PR review 都是可选项，只有风险足够时才启用。
- 大任务才默认进入需求收敛、任务拆分、worktree 隔离、TDD、subagent 并行和 PR-style review。
- 小任务不需要 `agents.md`，本仓也不创建 lowercase `agents.md`；现行入口是 `AGENTS.md` / `CLAUDE.md`。
- 对大任务，进入详细计划前可先做轻量 brainstorm / 方案比较，用于收敛方向；brainstorm 本身不得替代验收计划、任务拆分或验证。
- 不要把重流程套到琐碎编辑、纯文档清理、明显错字或单文件低风险修复上。

## 写入边界

- 业务实现前，先从 `work_area_registry.md` 选择 `target_work_area`。
- 如果目标不清楚，保持只读并列出候选工作区。
- 根目录文件只在明确的仓库治理、路由、安全或文档任务中修改。
- 除非用户明确指定跨工作区任务，不要在 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/` 或其他工作区之间交叉写入。
- 脏工作树必须先按用途分组并理解来源。不得为了方便处理而使用 `reset`、`clean` 或 `stash`。

## Git 规则

只有同时满足以下条件时，才可以执行 `git add` 和 `git commit`：

- 用户已在接受的计划中明确授权。
- diff 范围清晰。
- diff 只包含当前任务文件。
- commit message 已确认，或计划里已有精确模板。
- dirty tree 没有把无关用户改动混进本次提交范围。

执行 `push`、创建 PR、`merge`、`reset`、`clean`、`rebase`、`stash` 或人工 `git worktree remove` 前，必须停下等待人工确认。唯一例外是 repo-local `WorktreeRemove` hook 可按 `docs/git-worktree-policy.md` 清理 clean `owner=agent` ephemeral worktree，且必须使用非 force remove 并保留 branch。

## 连续执行

长任务治理见 `docs/continuous-execution.md`。

- active-task ledger 路径是 `.tmp/active-task/current.md`。
- 当前 repo 任务不得把 `C:\Users\apple\.claude\plans\*.md` 当作必须写入的运行态计划文件；计划审批通过 ExitPlanMode 或对话完成。
- 如确实需要计划落盘，只能写 repo 内 `.tmp/active-task/current.md`；写全局 `.claude\plans` 必须由用户显式提出。
- ledger 只是运行态记录。它不是规则文件，不是 learning，也不能授权危险操作。
- 不创建 `.claude/tasks/current.md` 或其他轻量任务便签。
- Stop hook 只能在 ledger 显示已接受计划未完成时做轻量提醒。
- StopFailure 只能记录失败上下文。
- hooks 不得自动派发任务、自动继续执行或修改业务文件。

## 模块准入

新增或扩展 repo-local 工作流模块、hooks、tools、Playwright 配置、验证脚本，或超出当前确认范围的 skills 前，先使用 `docs/module-admission.md`。

模块准入卡必须说明：解决什么、不解决什么、触发条件、读取路径、写入路径、是否安装依赖、是否运行浏览器、对 git/worktree/global/kb 的影响、如何禁用、如何删除、最小验证命令，以及为什么现有模块不足。

## 前端能力

Playwright 和 `frontend-polish-lite` 只属于 `claudecode`，且只用于 coding-only 前端验证。它们不进入全局 core，不进入 `kb`，不写全局 hooks，也不用于所有任务。

只在前端任务涉及 UI 交互、页面行为、截图、视觉回归、响应式布局或明显可访问性检查时使用。

## ECC 与 Superpowers

ECC 已剔除；不得作为默认能力或候选模块恢复。未来重新引入必须重新走模块准入卡。

Superpowers 不作为本仓默认 SessionStart 工作流。Superpowers/TDD 只能在完成模块准入并获得用户确认后，作为明确的任务级 coding 路由使用。
