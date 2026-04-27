# 模块准入

新增或扩展 repo-local workflow modules、hooks、tools、validation scripts、Playwright configuration，或当前已接受范围外的 skills 前，先读本文件。

## 准入卡模板

- 名称：
- 类型：
- 解决什么问题：
- 不解决什么问题：
- 触发条件：
- 会读哪些路径：
- 会写哪些路径：
- 是否安装依赖：
- 是否运行浏览器：
- 是否影响 git/worktree/global/kb：
- 如何禁用：
- 如何删除：
- 最小验证命令：
- 为什么不能用现有模块解决：
- 状态：

准入卡本身不能授权危险操作。

## 当前模块卡

### repo-explorer-agent-guard

- 名称：`repo-explorer-agent-guard`
- 类型：repo-local read-only agent and PreToolUse Agent hook。
- 解决什么问题：替代 built-in `Explore`，强制本仓只读探索使用中文 Todo、原生 `Read` 禁令和路径纪律；不暴露 Read，以避开 text/code `Read` 被自动附加 `pages` 的工具链问题。
- 不解决什么问题：不修复 Read 工具本身，不启用 Read normalizer，不迁移业务文件，不清理 worktree/branch。
- 触发条件：用户或 agent 需要本仓只读探索；或 Claude Code 发起 `Agent(Explore)`。
- 会读哪些路径：`Agent` hook 只读取 hook stdin payload；`repo-explorer` 按任务只读仓内代码、文档和配置。
- 会写哪些路径：hook 不写文件；`repo-explorer` 不写文件。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 global/kb；不清理 branch/worktree；只阻断 built-in `Explore` 调用。
- 如何禁用：从 `.claude/settings.json` 移除 `block-builtin-explore.ps1` 的 `PreToolUse` `Agent` hook entry。
- 如何删除：删除 `.claude/agents/repo-explorer.md`、`.claude/hooks/block-builtin-explore.ps1`，并移除相关文档记录。
- 最小验证命令：`claude agents list`；PowerShell / pwsh parse hook；模拟 `Agent=Explore` 被阻断；模拟 `Agent=repo-explorer` 不阻断。
- 为什么不能用现有模块解决：built-in `Explore` 未稳定遵守中文 Todo 与 text/code 原生 `Read` 禁令；已禁用的 Read hook 不应重新作为 normalizer。
- 状态：已接受 repo-local read-only exploration guard。

### continuous-execution-ledger

- 名称：`continuous-execution-ledger`
- 类型：ignored runtime ledger
- 解决什么问题：记录当前长任务计划、进度、下一步、blocker 和 resume notes。
- 不解决什么问题：权限授予、危险操作审批、任务调度、learning 晋升。
- 触发条件：已接受的多步计划、长任务、上下文风险任务、中断后恢复。
- 会读哪些路径：`.tmp/active-task/current.md`、`docs/continuous-execution.md`
- 会写哪些路径：`.tmp/active-task/current.md`
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：只写 ignored 文件；不影响 git/global/kb。
- 如何禁用：停止读取或更新 ledger。
- 如何删除：删除 `.tmp/active-task/current.md`。
- 最小验证命令：`git check-ignore -v .tmp/active-task/current.md`
- 为什么不能用现有模块解决：根文档描述规则，但不保存运行态进度。
- 状态：已接受为 repo-local runtime 用法。

### stop-guard-lite

- 名称：`stop-guard-lite`
- 类型：候选 Stop / StopFailure hook。
- 解决什么问题：当已接受计划看起来未完成时提醒 agent。
- 不解决什么问题：调度、自动继续、权限绕过、业务编辑、依赖安装。
- 触发条件：未来明确批准添加 Stop hook；ledger 存在且显示工作未完成。
- 会读哪些路径：`.tmp/active-task/current.md`
- 会写哪些路径：StopFailure 只有明确批准后才可追加本地 ignored diagnostics；Stop 必须不写入。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 git/worktree/global/kb。
- 如何禁用：批准后从 repo-local settings 移除 hook 注册；或不批准。
- 如何删除：批准后删除 hook script 和 settings entry。
- 最小验证命令：用 sample ignored ledger 做 dry-run。
- 为什么不能用现有模块解决：规则和 skills 依赖 agent 自律；轻量 Stop reminder 用于捕捉意外过早停止。
- 状态：仅提案；没有新的用户确认前不得写 hook。

### read-text-pages-guard

- 名称：`read-text-pages-guard`
- 类型：disabled / experimental repo-local PreToolUse hook。
- 解决什么问题：将 Markdown/text/code 文件 Read 调用里的 unsupported `pages` 字段从 `updatedInput` 移除，避免 Read 被工具系统判定失败后触发错误写入 fallback。
- 不解决什么问题：不读取文件、不写计划文件、不替代正常 Read、不处理 PDF Read 的 pages。
- 触发条件：PreToolUse `Read`，且 `file_path` 后缀是 `.md`、`.txt`、`.json`、`.py`、`.ps1`、`.html`、`.css`、`.js`、`.ts`、`.tsx`、`.jsx`、`.yaml` 或 `.yml`，并且 tool input 包含 `pages` 字段。
- 会读哪些路径：无；只读取 hook stdin payload。
- 会写哪些路径：无。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 worktree/global/kb；只在当前 repo 的 `.claude/settings.json` 注册 repo-local hook。
- 如何禁用：从 `.claude/settings.json` 移除对应 `PreToolUse` `Read` hook entry。
- 如何删除：删除 `.claude/hooks/block-read-pages-for-text.ps1` 并移除 settings entry。
- 最小验证命令：用 JSON payload 模拟 `Read` Markdown with empty `pages`，确认 exit 0 且 stdout JSON 含不带 `pages` 的 `updatedInput`；模拟普通 Markdown Read 和 PDF pages Read，确认 exit 0 且不修改。
- 为什么不能用现有模块解决：文档规则依赖模型自律，blocking hook 会导致 Read 失败并放大 fallback 写入风险。
- 状态：disabled / experimental；真实 Claude Code 会话仍出现 `pages: ""` invalid parameter，且 hook debug log 未记录触发，说明当前 CC/VS 面板 Read 参数问题不能靠 blocking/normalizing hook 稳定修复。本仓主线程和 subagent 对 text/code 文件禁用原生 `Read`；业务修改前必须先用 `repo-explorer`、`Grep` / `Glob` / `Bash` 查看片段。如果 `Edit` / `Write` 因没有成功原生 `Read` 登记而不可用，只能报告 blocker；只有用户明确回复“授权 scripted patch plan 修改 <file>”后，才允许脚本化补丁。

### scan-agent-worktrees

- 名称：`scan-agent-worktrees`
- 类型：repo-local PowerShell tool and explicit destructive slash command。
- 解决什么问题：为唯一 agent worktree / branch 清理入口 `/scan-agent-worktrees` 提供扫描、clean agent auto worktree 删除、zero-ahead `wt-auto-*` agent branch 删除。
- 不解决什么问题：不删除 dirty worktree、不删除 user/protected worktree 或 branch、不清理 legacy 或非 auto root worktree、不删除有 unique commits / checked-out / upstream / origin 同名远端的 branch、不放开裸 git 命令。
- 触发条件：用户运行 `/scan-agent-worktrees`；该调用即视为授权执行受控清理。
- 会读哪些路径：`git worktree list --porcelain`、`git branch --list "wt-auto-*"`、`git status --porcelain`、git branch upstream/origin refs、`C:\Users\apple\_worktrees\claudecode\.registry\*.json`。
- 会写哪些路径：只通过 `git -C C:\Users\apple\claudecode worktree remove -- <path>` 移除符合 contract 的 linked worktree checkout；只通过 `git -C C:\Users\apple\claudecode branch -d <branch>` 删除符合 contract 的 local branch；不写 repo 文件。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：只影响本仓符合条件的 local linked worktree 和 local branch；不影响 global/kb；`.claude/settings.local.json` 仅作为 ignored 本地权限配置。
- 如何禁用：不运行 slash command；或删除 `.claude/commands/scan-agent-worktrees.md`。
- 如何删除：删除命令文件和 `.claude/tools/worktree-governor/scan_agent_worktrees.ps1`。
- 最小验证命令：PowerShell / pwsh parse；真实运行 `/scan-agent-worktrees`；临时 repo fixture 验证 clean agent auto worktree 会移除，zero-ahead agent branch 会删除，dirty/user/protected/非 auto root/unique commits/pushed-upstream 会 skipped。
- 为什么不能用现有模块解决：只读扫描和独立 cleanup 会造成授权拆分及权限弹窗；裸 git 命令缺少 marker、auto root、clean status、zero-ahead、upstream/origin 和 owner/protected 护栏。
- 状态：已接受 repo-local explicit cleanup command/tool；`/cleanup-agent-worktrees` 已退役，不作为常规入口。

### ship-task-pr

- 名称：`ship-task-pr`
- 类型：repo-local PowerShell tool and slash command。
- 解决什么问题：把当前任务改动通过受控 wrapper 整理成 branch、commit、`git push -u origin <branch>` 和 GitHub PR。
- 不解决什么问题：不做 worktree/branch 清理、不复用 `/scan-agent-worktrees`、不绕过用户 staging 意图、不放开裸 `git push`。
- 触发条件：用户显式运行 `/ship-task-pr <title>` 或 `/ship-task-pr --branch <branch-name> --title "<title>"`。
- 会读哪些路径：当前 repo 的 `git status`、staged diff、unstaged diff、`origin` remote、base branch、`gh --version`。
- 会写哪些路径：当前任务 staged 文件对应的 commit、必要时创建本地 branch、`.tmp/pr/<branch>/body.md`、远端 `origin/<branch>` 和 GitHub PR；本地权限配置只写 ignored `.claude/settings.local.json`。
- 是否安装依赖：否；要求现有 `git` 和 `gh` 可用。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：影响本仓 git branch / commit / remote branch / PR；不删除 branch/worktree，不修改 global/kb。
- 如何禁用：删除 `.claude/commands/ship-task-pr.md`，并从 `.claude/settings.local.json` 移除对应 wrapper allow。
- 如何删除：删除 `.claude/tools/pr/ship_task_pr.ps1`、`.claude/commands/ship-task-pr.md` 和相关文档记录；保留历史 commit/PR 由 GitHub/Git 管理，不由工具清理。
- 最小验证命令：PowerShell / pwsh parse；`-DryRun`；slug 生成；危险 branch 拒绝；无 staged diff 停止；blocked paths 不会被 stage；`git diff --check`；`git diff --cached --check`。
- 为什么不能用现有模块解决：`/scan-agent-worktrees` 只承载 agent worktree / branch 受控清理，不能承载 push/PR；裸 git 命令缺少本仓提交范围、blocked path 和远端写入护栏。
- 状态：已接受 repo-local explicit remote-write command/tool。

### local-methodology-entrypoints

- 名称：`local-methodology-entrypoints`
- 类型：repo-local slash commands and policy doc。
- 解决什么问题：在不全量安装 Superpowers 的前提下，为 brainstorm、TDD 和本地 PR Review 提供轻量入口。
- 不解决什么问题：不安装 Superpowers、不启用 SessionStart hook、不启用插件 `code-reviewer`、不替代任务验收。
- 触发条件：用户运行 `/brainstorm-task`、`/tdd-task` 或 `/review-pr-local`。
- 会读哪些路径：`docs/agent-methodology-policy.md`、相关任务文件、当前 git diff；TDD 时读取目标工作区现有测试入口。
- 会写哪些路径：`/brainstorm-task` 默认不写；`/tdd-task` 只在明确开发/修 bug 时写当前任务文件；`/review-pr-local` 默认不写，可选只写 ignored `.tmp/pr-review/<branch>.md`。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 global/kb；默认不创建 branch/worktree；`/review-pr-local` 只读 git，不 push、不提交。
- 如何禁用：不运行对应 slash command；或删除 `.claude/commands/brainstorm-task.md`、`.claude/commands/tdd-task.md`、`.claude/commands/review-pr-local.md`。
- 如何删除：删除命令文件、`.claude/agents/local-pr-reviewer.md`、`.claude/tools/pr/review_local_pr.ps1` 和 `docs/agent-methodology-policy.md` 中对应说明。
- 最小验证命令：PowerShell / pwsh parse `review_local_pr.ps1`；运行脚本默认 `-NoWrite`；`git diff --check`；`git diff --cached --check`。
- 为什么不能用现有模块解决：现有 `review-diff` 是 skill；本轮需要 Claude Code slash command、本地只读 agent 和明确替代云端 Codex PR Review 的入口。
- 状态：已接受 repo-local methodology command set。

### local-pr-reviewer

- 名称：`local-pr-reviewer`
- 类型：repo-local read-only agent and optional PowerShell helper。
- 解决什么问题：用本地只读 agent 审查当前分支相对 base 的 git diff，替代云端 Codex PR Review。
- 不解决什么问题：不改代码、不提交、不 push、不向 GitHub 发布 review、不 merge。
- 触发条件：用户运行 `/review-pr-local`，或明确要求本地只读 PR/diff 审查。
- 会读哪些路径：`git status --short`、`git rev-parse --abbrev-ref HEAD`、`git diff --stat <base>...HEAD`、`git diff --name-status <base>...HEAD`、`git diff <base>...HEAD`、`git log --oneline <base>..HEAD`，以及 diff 涉及文件。
- 会写哪些路径：默认无；用户显式要求时只写 ignored `.tmp/pr-review/<branch>.md`。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：只读 git；不影响 worktree/global/kb。
- 如何禁用：不运行 `/review-pr-local`；或删除 `.claude/commands/review-pr-local.md`。
- 如何删除：删除 `.claude/agents/local-pr-reviewer.md`、`.claude/commands/review-pr-local.md`、`.claude/tools/pr/review_local_pr.ps1` 和相关文档记录。
- 最小验证命令：PowerShell / pwsh parse；运行 `.claude\tools\pr\review_local_pr.ps1 -Base origin/main` 默认 `-NoWrite`，确认 `.tmp/pr-review` 不新增文件。
- 为什么不能用现有模块解决：云端 Codex PR Review 不再作为默认审查面；本仓需要一个只读、本地、可检查 diff 输入的替代入口。
- 状态：已接受 repo-local read-only review agent/tool。

### resume-active-task

- 名称：`resume-active-task`
- 类型：repo-local skill。
- 解决什么问题：从 ignored ledger 和当前 git state 恢复暂停任务。
- 不解决什么问题：绕过安全边界、批准危险 git、修改 global/kb。
- 触发条件：用户说 resume、继续上个任务，或上下文中断。
- 会读哪些路径：`.tmp/active-task/current.md`、`AGENTS.md`、`CLAUDE.md`、`docs/continuous-execution.md`、`git status`
- 会写哪些路径：常规任务路由允许后，写 `.tmp/active-task/current.md` 和当前任务文件。
- 是否安装依赖：否。
- 是否运行浏览器：默认否。
- 是否影响 git/worktree/global/kb：不影响 global/kb；git 默认只读，除非另行批准。
- 如何禁用：不调用该 skill。
- 如何删除：删除 `.claude/skills/resume-active-task/`。
- 最小验证命令：`Get-Content .tmp/active-task/current.md -ErrorAction SilentlyContinue`
- 为什么不能用现有模块解决：通用任务路由不处理 resume 专用状态对账。
- 状态：已接受 repo-local skill。

### module-admission

- 名称：`module-admission`
- 类型：repo-local skill 和文档模板。
- 解决什么问题：防止临时新增 module、hook、tool 或 Playwright 能力。
- 不解决什么问题：已准入模块的具体实现。
- 触发条件：新增 workflow module、hook、tool、validation script、Playwright config 或新 skill。
- 会读哪些路径：`docs/module-admission.md`、相关模块文档。
- 会写哪些路径：准入提案文本；只有任务明确要求更新准入记录时才写 tracked docs。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 global/kb。
- 如何禁用：只有用户明确把任务限定为琐碎 docs-only 编辑时才可跳过。
- 如何删除：删除 `.claude/skills/module-admission/`。
- 最小验证命令：按 required fields 检查已完成卡片。
- 为什么不能用现有模块解决：现有文档不强制统一 pre-write card。
- 状态：已接受 repo-local skill。

### frontend-polish-lite

- 名称：`frontend-polish-lite`
- 类型：repo-local skill。
- 解决什么问题：轻量前端 UI polish 和 validation。
- 不解决什么问题：完整 design system、产品重设计、依赖设置、所有任务视觉 QA。
- 触发条件：前端任务、UI interaction、page behavior、screenshot、responsive check、visual regression、accessibility smoke check。
- 会读哪些路径：变更的前端文件、最近的 README/design notes、`docs/frontend-validation.md`、`docs/playwright-policy.md`
- 会写哪些路径：只有用户要求实现时，才写当前任务前端文件。
- 是否安装依赖：否。
- 是否运行浏览器：只有 headed、screenshot 或 trace validation 需要时运行。
- 是否影响 git/worktree/global/kb：不影响 global/kb；除非另行计划，否则不影响 worktree。
- 如何禁用：不调用该 skill。
- 如何删除：删除 `.claude/skills/frontend-polish-lite/`。
- 最小验证命令：`playwright --version`
- 为什么不能用现有模块解决：`frontend-patterns` 覆盖 coding patterns，但不提供聚焦 UI polish 的验收循环。
- 状态：已接受 repo-local skill。

### review-diff

- 名称：`review-diff`
- 类型：repo-local skill。
- 解决什么问题：统一 diff review 和 verification report。
- 不解决什么问题：GitHub PR 创建、merge、push 或自动批准。
- 触发条件：用户要求 review、提交前、中/大 patch 后。
- 会读哪些路径：`git status`、`git diff`、变更文件、相关 docs/tests。
- 会写哪些路径：默认只写 review report；只有用户要求修复时才改文件。
- 是否安装依赖：否。
- 是否运行浏览器：默认否。
- 是否影响 git/worktree/global/kb：git 只读；不影响 global/kb。
- 如何禁用：不调用该 skill。
- 如何删除：删除 `.claude/skills/review-diff/`。
- 最小验证命令：`git diff --stat`
- 为什么不能用现有模块解决：`requesting-code-review` 面向发起审查请求；本模块提供本地 diff review 清单。
- 状态：已接受 repo-local skill。

### maintenance learning

- 名称：`/maintenance learning`
- 类型：repo-local skill mode。
- 解决什么问题：把本地 raw error cache 分组整理为经过用户审查的 repo learning 候选。
- 不解决什么问题：自动 global learning、自动更新 kb、提交 raw logs、把 `LEARNINGS` 晋升到 docs / skills / entry。
- 触发条件：用户要求精炼 `ERRORS → LEARNINGS` 候选，或任务产生重复 repo-local errors 后需要定期维护。
- 会读哪些路径：`.learnings/ERRORS.md`、`.learnings/LEARNINGS.md`、`.claude/tools/learning-loop/**`。
- 会写哪些路径：默认不写；用户确认候选后才允许另行写 `.learnings/LEARNINGS.md`。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 global/kb。
- 如何禁用：不调用 `/maintenance learning`。
- 如何删除：修改 `.claude/skills/maintenance/SKILL.md` 移除 learning mode。
- 最小验证命令：`git check-ignore -v .learnings/ERRORS.md`
- 为什么不能用现有模块解决：旧独立 learning promotion skill 已退役；`/promote-learning` 只处理 `LEARNINGS → docs / skills / entry`。
- 状态：已接受 repo-local skill mode。

### playwright-config-or-script

- 名称：`playwright-config-or-script`
- 类型：未来模块候选。
- 解决什么问题：为特定工作区提供可重复的前端验证命令。
- 不解决什么问题：依赖安装、全局验证、kb 验证。
- 触发条件：某个前端工作区需要比 ad hoc CLI 更稳定的重复 Playwright validation。
- 会读哪些路径：目标工作区前端文件、本地 app 启动文档、`docs/playwright-policy.md`。
- 会写哪些路径：未来准入卡明确命名的 repo-local config 或 script 路径。
- 是否安装依赖：默认否；安装必须另行确认。
- 是否运行浏览器：是，仅用于获批验证任务。
- 是否影响 git/worktree/global/kb：只影响获批 tracked repo-local 文件；不影响 global/kb。
- 如何禁用：停止使用该 script/config。
- 如何删除：删除获批的 config/script 文件。
- 最小验证命令：由未来目标工作区定义。
- 为什么不能用现有模块解决：在需要稳定命令前，ad hoc CLI 已足够。
- 状态：写入任何 config 或 script 前必须另给 future card。

### agent-branch-sweep

- 名称：`agent-branch-sweep`
- 类型：repo-local manual PowerShell tool。
- 解决什么问题：在 ephemeral worktree 已移除后，安全处理 registry 证明的 agent 临时分支。
- 不解决什么问题：worktree cleanup、branch 强删、user/direct/protected branch 清理、remote branch 清理、自动 hook cleanup。
- 触发条件：用户明确要求检查或清理 agent `wt-auto-*` branches。
- 会读哪些路径：`C:\Users\apple\_worktrees\claudecode\.registry\*.json`、`git worktree list --porcelain`、local/remote git refs。
- 会写哪些路径：默认不写；显式 `-Apply` 只可能通过 `git branch -d <branch>` 删除符合 contract 的本地 branch。`-ArchivePlan` 只输出计划，不写 tag/bundle/patch。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：只影响本仓 local git branches；不影响 worktree/global/kb，不接入 hooks。
- 如何禁用：不运行该脚本。
- 如何删除：删除 `.claude/tools/worktree-governor/sweep_agent_branches.ps1` 并移除相关文档。
- 最小验证命令：`powershell.exe` / `pwsh` parse；在临时 repo 验证 zero-ahead delete、unique-ahead needs-review、checked-out/user/protected/remote branch skip。
- 为什么不能用现有模块解决：`WorktreeRemove` 必须保留 branch；`ccw-*` 是 legacy/manual，且没有 registry-first branch safety contract。
- 状态：已接受为 repo-local manual tool；不接 hook。

### claudecode-ecc-retired

- 名称：`claudecode-ecc-retired`
- 类型：retired capability record。
- 解决什么问题：明确 ECC 已从 claudecode active 规则层剔除。
- 不解决什么问题：不提供 ECC cleanup workflow，不恢复 ECC，不处理全局历史 archive/cache。
- 触发条件：无默认触发；未来重新引入 ECC 必须另开任务并重新走模块准入卡。
- 会读哪些路径：仅在明确审计任务中读取 repo-local 规则层。
- 会写哪些路径：无默认写入。
- 是否安装依赖：否。
- 是否运行浏览器：否。
- 是否影响 git/worktree/global/kb：不影响 worktree/global/kb。
- 如何禁用：保持不调用、不注册、不安装。
- 如何删除：保留本退休记录即可；不得把它恢复为候选模块。
- 最小验证命令：在 repo-local 规则层搜索 `ECC` / `Enhanced Context Craft`，确认没有 active hook、tool、skill 或 settings 配置。
- 为什么不能用现有模块解决：这是退休边界记录，不是可运行模块。
- 状态：已剔除；不得作为默认能力或候选模块恢复。
