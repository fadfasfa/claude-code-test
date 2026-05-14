# claudecode Codex 规则

`claudecode` 是个人总编程仓、多子项目母仓和 Codex 编程主执行仓。它不是单项目仓库。

Codex 是当前唯一主流程。`CLAUDE.md` 与 `.claude/README.md` 仅为空白占位，不作为当前规则来源。

## 默认规则

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 仓库根目录是治理、路由和工具骨架，不是默认业务写入面。
- 所有业务修改必须先落到明确子项目或已登记工作区；业务写入范围以 `work_area_registry.md` 为准。
- Windows 默认使用 PowerShell。
- 默认使用单一 active worktree，不并发开启多个任务分支。
- 不自动创建长期 worktree；不自动清理 branch / worktree。
- 优先做小步、直接、可局部理解的修改；不做顺手重构。
- 注释只解释“为什么”和边界条件，不复述代码“做什么”。

## Git 与发布边界

- 不默认执行 `git add`、`git commit`、`git push`、`git clean`、`git reset`、`git rebase`、`git stash`。
- commit 必须得到用户明确授权。
- push、PR、merge 必须得到用户明确授权，并在执行前二次确认。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。
- `main` 只作为基准，不作为默认任务执行面。

## 代码任务规则

- 所有代码任务必须触发 `karpathy-project-bridge`，并遵守全局 `karpathy-guidelines`。
- 前端 UI 任务必须触发 `frontend-design-project-bridge`，并遵守全局 `frontend-design`；同时仍触发 `karpathy-project-bridge`。
- Superpowers 是方法论路由和能力索引，不得覆盖本文件、`work_area_registry.md`、Git 边界、安全边界、发布权限或验收规则。
- 修改代码后必须运行最小有效验证；无法验证必须说明具体原因。
- 完成前默认使用 `repo-verification-before-completion` 口径报告证据。

## Methodology Router

- `brainstorming`：目标、方案或取舍不清时触发；只做问题收敛和方案对比，不写文件。
- `using-git-worktrees`：任何非琐碎写入任务开始前触发；默认通过 `scripts/workflow/worktree-start.ps1` 创建 detached active worktree。
- `writing-plans`：用户要求计划、任务跨多个文件或需要分阶段交付时触发；计划不得替代执行授权。
- `TDD`：有可测试接口、bug fix 或行为变更时触发；没有 TDD 时必须说明等价自动化测试或降级验收原因。
- `debugging`：根因不清、测试失败或工具链异常时触发；先只读缩小问题面，再改文件。
- `executing-plans`：用户明确要求实施已确认计划时触发；按计划边界小步落地，偏离计划需说明。
- `requesting-code-review`：进入 finalize 前触发；必须运行 `local-review.ps1` 并记录 acceptance gate。

## 保护资产

- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、私有配置、`.env`、`auth.json`、`local.yaml`、`proxies.json`。
- 未选定业务工作区、用户级 `.claude` / `.codex`、KB 仓库默认不写。
- `run/**` 不再整体禁止；明确授权的普通代码任务可按 `work_area_registry.md` 执行。
- `run/**` 中的 raw data、原始抓取结果、不可重建资产、敏感配置和当前脏树默认受保护。
- 任何备份失败都必须立即停止，不得继续执行删除、覆盖、移动或其他破坏性动作。

## Worktree 规则

- 默认使用单一 active worktree。
- 任务 worktree 放在 `C:\Users\apple\worktrees\` 下，不放进仓库内部。
- 写入任务开始先创建 detached active worktree。
- 通过验收后，才在明确授权下创建 `codex/<area>/<task-slug>` review branch。
- 不并发开启多个任务分支，除非用户明确要求。
- worktree 不设硬数量上限，但任务完成后应及时进入清理候选。
- 超过 72 小时的无用 worktree 应进入清理候选。
- 不自动删除 worktree 或分支，除非用户明确要求。

## Skill 规则

- `.agents/skills/README.md` 是仓库级 Codex skill 白名单入口。
- 当前允许的仓库级 skill 由白名单列出。
- 不恢复 command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。

## 分级验收

- 所有任务必须生成或更新 `TASK_HANDOFF.md` 和 `.task-worktree.json`。
- `.task-worktree.json` 固定 schema 缺字段时，`worktree-status`、`local-review`、`finalize-pr`、`cleanup-worktree` 必须失败。
- acceptance gate 只有三档：`automated`、`recommended-manual`、`manual-required`。
- 低风险代码任务只有 changed files 全在 `target_paths` / `allowed_paths` 内、verify 通过、local-review 无 high-risk finding 时，才能自动验收。
- UI / 视觉、raw data、全局配置、AGENTS / skills / workflow scripts / Git 发布流程、删除移动、大范围重构、测试不足、主仓 dirty overlap 都必须人工验收。
- `TASK_HANDOFF.md` 是可验收证据包，不代表每次都必须打开 VS Code。

## 完成报告

每轮非琐碎任务收尾必须说明：

- 修改文件。
- 是否触碰 `run/**`。
- 是否执行删除、清理或移动。
- 是否 staging、commit 或 push。
- diff 摘要。
- 验证命令与结果；无法验证时说明原因。
- acceptance gate 与可选 VS Code 审查命令。
