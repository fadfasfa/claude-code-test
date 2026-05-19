# Claude Code Entry

本文件是 Claude Code 在本仓的独立入口。Claude Code 可以直接完成探查、修改、验证和提交；Codex 也可以独立工作，二者互不依赖。

## Default Flow

- 默认使用简体中文。
- 开始任务先运行 `git status --short`，发现非本轮修改时先报告并避免混入。
- 先读 `PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档，再选择目标工作区。
- 修改保持小步、明确范围，不顺手重构业务代码。
- 修改后运行最小有效验证；无法验证时说明具体原因。
- 验证通过后，若本轮允许提交，只暂存本轮修改文件并 commit；禁止 `git add .`。

## Independent Work

- Claude Code 不需要调用 Codex 才能读取、修改或验证仓库文件。
- Codex 不需要通过 Claude Code 才能执行任务。
- OpenAI Codex plugin 可以作为可选辅助或第二意见来源，但不是主流程要求。
- 不再强制 Claude Code 计划、Codex 执行、Claude Code 验收的固定分工。

## Safety

- 不读取或修改 `.env`、`auth.json`、`local.yaml`、`proxies.json`、token、cookie、API key 或 proxy secret。
- 禁止 `git push`，除非用户明确单独要求。
- 禁止 `git reset --hard`、`git clean -fdx`、大范围删除和不可逆清理，除非用户明确批准。
- 删除、覆盖、移动前先确认目标路径；需要备份时先确认备份成功。

## Retired Workflow Note

旧 CC-CX 强编排、Guard 状态机和 break-glass 流程已经退役，不作为 Claude Code 日常入口。
