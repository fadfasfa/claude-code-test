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
