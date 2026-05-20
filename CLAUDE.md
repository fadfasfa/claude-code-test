# Claude Code Entry

本文件是 Claude Code 在本仓的独立入口。Claude Code 可以直接完成探查、修改、验证和提交；Codex 也可以独立工作，二者互不依赖。

## Default Flow

- 默认使用简体中文。
- 开始任务先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
- 只读探查可以直接执行；凡涉及非只读探查、非平凡文件修改、workflow/config/skill/hook 修改、git 写操作、worktree 操作或破坏性命令，必须先输出计划并等待用户确认。
- 普通极小单文件修改若不涉及 workflow/config/skill/hook、git 写、worktree 或破坏性操作，且用户当前轮明确要求直接执行，可以跳过计划确认；仍需按授权范围小步修改并验证。
- 计划必须包含：`git status`、预计修改文件、修改内容、不修改范围、验证命令、Git 处理方式；确认后按计划小步执行，范围变化时重新确认。
- 先读 `PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档，再选择目标工作区。
- 修改保持小步、明确范围，不顺手重构业务代码。
- 修改后运行最小有效验证；无法验证时说明具体原因。
- 验证通过后，若本轮允许提交，只暂存本轮修改文件并 commit；禁止 `git add .`。

## Independent Work

- Claude Code 不需要调用 Codex 才能读取、修改或验证仓库文件。
- Codex 不需要通过 Claude Code 才能执行任务。
- 即使 OpenAI Codex plugin 可用，Claude Code 也不得在无用户当前轮显性命令时调用、委派、审查或触发 Codex / CX。
- 不再强制 Claude Code 计划、Codex 执行、Claude Code 验收的固定分工。

## Safety

- 不读取或修改 `.env`、`auth.json`、`local.yaml`、`proxies.json`、token、cookie、API key 或 proxy secret。
- 禁止 `git push`，除非用户明确单独要求。
- 禁止 `git reset --hard`、`git clean -fdx`、大范围删除和不可逆清理，除非用户明确批准。
- 删除、覆盖、移动前先确认目标路径；需要备份时先确认备份成功。

## Retired Workflow Note

旧 CC-CX 强编排、Guard 状态机和 break-glass 流程已经退役，不作为 Claude Code 日常入口。
