# Independent Agent Workflow

本文件是当前 Claude Code 与 Codex 的独立工作流说明。两者都可以直接完成任务；不要求一方调度另一方。

## Claude Code 独立工作流

1. 先运行 `git status --short`，确认是否存在非本轮修改。
2. 读取 `CLAUDE.md`、`PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档。
3. 从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. 小步修改；不混入无关重构、格式化或业务目录整理。
5. 运行最小有效验证；无法验证时说明具体原因。
6. 验证通过后，按本轮授权只暂存本轮修改文件并 commit。

## Codex 独立工作流

1. 先运行 `git status --short`，确认是否存在非本轮修改。
2. 读取 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档。
3. 从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. 直接探查、实现和验证；不需要 Claude Code 计划或验收才能工作。
5. 验证通过后，按本轮授权只暂存本轮修改文件并 commit。

## 自动 Commit 规则

- commit 前必须有验证结果或明确的无法验证说明。
- 只允许 `git add` 本轮修改文件，禁止 `git add .`。
- commit message 应描述本轮目标，不混入非本轮修改。
- `git push` 永远需要用户单独明确要求。

## 高危操作确认

- `git reset --hard`、`git clean -fdx`、大范围删除、批量移动、不可逆清理必须先得到用户明确批准。
- 删除、覆盖、移动前确认目标路径；需要备份时先确认备份成功。
- 不读取或修改凭据、token、cookie、API key、proxy secret 或私有配置。
- `run/**`、`QuantProject/**` 的业务逻辑只在明确任务范围内修改。

## Retired Workflow Note

不再强制 Claude Code 计划、Codex 执行、Claude Code 验收。旧 CC-CX 强编排、Guard v4、break-glass 和状态机名 `NORMAL`、`CX_DEGRADED`、`CC_BG_READ`、`CC_BG_WRITE` 只作为历史术语保留，不作为当前主流程要求。

## Ultraplan 预留

后续 Ultraplan 可作为复杂任务计划入口和浏览器审查辅助。小任务仍由 Claude Code 或 Codex 独立完成。
