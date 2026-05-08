# claudecode Codex 规则

`claudecode` 是多工作区本地开发仓库。Codex 是当前唯一主流程。

`CLAUDE.md` 与 `.claude/README.md` 仅为空白占位，不作为当前规则来源。

## 默认规则

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 修改前先确认目标工作区；业务写入范围以 `work_area_registry.md` 为准。
- 仓库根目录只承载治理文档、路由文档和入口说明，不作为默认业务写入区。
- Windows 默认使用 PowerShell。
- 优先做小步、直接、可局部理解的修改；不做顺手重构。
- 注释只解释“为什么”和边界条件，不复述代码“做什么”。

## 禁止默认触碰

- 不读取或修改凭据、token、auth、cookie、API key、proxy、私有配置。
- 不默认修改 `run/**`、`scripts/**`、`.codex/**`、用户级 `.claude` / `.codex`、KB 仓库。
- 不默认执行 `git add`、`git commit`、`git push`、`git clean`、`git reset`、`git rebase`、`git stash`。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## Worktree 规则

- `main` 只作为基准，不作为任务执行面。
- 任务 worktree 放在 `C:\Users\apple\worktrees\` 下，不放进仓库内部。
- 只读探查优先用 detached worktree。
- 写入任务才创建短生命周期 `codex/<task>` 分支。
- worktree 不设硬数量上限，但任务完成后应及时清理。
- 超过 72 小时的无用 worktree 应进入清理候选。
- 不自动删除 worktree 或分支，除非用户明确要求。

## Skill 规则

- `.agents/skills/README.md` 是唯一仓库级 Codex skill 白名单入口。
- 当前只保留 `repo-module-admission` 与 `repo-verification-before-completion`。
- 不新增 skill，除非用户明确要求。
- 不恢复 command、hook、memory、learning promotion、PR、task resume 或 worktree 高权限 skill。

## 完成报告

每轮非琐碎任务收尾必须说明：

- 修改文件。
- 是否触碰 `run/**`。
- 是否执行删除、清理或移动。
- 是否 staging、commit 或 push。
- diff 摘要。
- 验证命令与结果；无法验证时说明原因。
