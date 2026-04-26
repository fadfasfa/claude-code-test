# claudecode Agent 规则

`claudecode` 是一个多功能开发仓库。它包含多个相互独立的业务工作区，也包含本仓 repo-local 的 Claude Code 工作流规则。仓库根目录是治理与任务路由控制面，不是默认业务实现目录。

## 规则读取链

通用 agent 或 Codex 风格会话在改代码或改工作流文件前，先读这些 repo-local 文件。Claude Code 会话从 `CLAUDE.md` 进入，再跟随本文件；两者属于同一条规则链，只是入口不同。

1. `AGENTS.md`
2. `CLAUDE.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. 相关 `docs/` 文件

入口文件不是每次都要全量读取。普通任务优先读取中文头部简介、目录、目标工作区相关段落和相关小节；已知 `target_work_area` 时，只搜索该工作区相关段落。不要默认 `Read` `AGENTS.md`、`PROJECT.md` 或 `work_area_registry.md` 的 1-2000 行，也不要把入口链全文件读取当作启动固定动作。`Read` 因参数、行号或范围失败一次后，不得用相同参数重试，应改用 `rg`、`Select-String`、`Get-Content -TotalCount` 或小范围行读取。

本仓不继承 `kb` 工作流。只有用户明确要求做边界对比或污染风险检查时，才可只读参考 `kb`。不得从本仓工作流修改 `kb`。

本仓也不修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App、Codex Proxy、全局 hooks 或全局 skills；除非用户另起一个全局层任务。

## 任务路由

详细路由规则见 `docs/task-routing.md`。

- 小任务和明确 bug 修复走轻量路径：确认目标工作区，读取窄范围文件，修改，运行最近的有效验证，报告结果。
- 中任务走简短计划和明确验证。TDD、worktree、subagent、PR review 都是可选项，只有风险足够时才启用。
- 大任务才默认进入需求收敛、任务拆分、worktree 隔离、TDD、subagent 并行和 PR-style review。
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
- ledger 只是运行态记录。它不是规则文件，不是 learning，也不能授权危险操作。
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
