# Independent Agent Workflow

本文件是当前 Claude Code 与 Codex 的独立工作流说明。两者都可以直接完成任务；不要求一方调度另一方。

## Claude Code 独立工作流

1. 先运行 `git status --short`，确认是否存在非本轮修改。
2. 读取 `CLAUDE.md`、`PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档。
3. 从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. 只读探查可以直接执行；用户当前轮明确要求实现、修复、调整或修改时，普通仓库文件编辑可以直接执行，仍需按授权范围小步修改并验证。
5. `S/M/L`、worktree、计划、验证、review 和收尾流程只引用 `AGENTS.md` 的薄边界；当前轮已明确授权或计划已批准时，不重复要求业务层确认。
6. 小步修改；不混入无关重构、格式化或业务目录整理。
7. 运行最小有效验证；无法验证时说明具体原因。
8. 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才只暂存本轮修改文件并 commit。

## Codex 独立工作流

1. 先运行 `git status --short`，确认是否存在非本轮修改。
2. 读取 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和任务相关 workflow 文档。
3. 从 `docs/workflows/work_area_registry.md` 选择目标工作区。
4. 只读探查可以直接执行；用户当前轮明确要求实现、修复、调整或修改时，普通仓库文件编辑可以直接执行，仍需按授权范围小步修改并验证。
5. `S/M/L`、worktree、计划、验证、review 和收尾流程只引用 `AGENTS.md` 的薄边界；当前轮已明确授权或计划已批准时，不重复要求业务层确认。
6. 直接实现和验证；不需要 Claude Code 计划或验收才能工作。
7. 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才只暂存本轮修改文件并 commit。

## Commit 授权规则

- 默认停在已验证 diff；commit 必须有当前轮明确授权。
- commit 前必须有验证结果或明确的无法验证说明。
- 只允许 `git add` 本轮修改文件，禁止 `git add .`。
- commit message 应描述本轮目标，不混入非本轮修改。
- push 不由 commit 隐含；用户明确要求 push 后，agent 自行执行并验证远端状态，不要求用户手动输入命令。

## 高危操作确认

- push、PR、merge 或 discard 未获明确授权时禁止主动执行；用户明确要求后由 agent 按确认范围完整执行并验证结果，不得要求用户手动输入命令。
- `git reset --hard`、`git clean -fdx`、`git restore`、覆盖式 checkout、大范围删除、批量移动、不可逆清理必须先得到用户明确批准。
- 删除、覆盖、移动前确认目标路径；需要备份时先确认备份成功。
- 不读取或修改凭据、token、cookie、API key、proxy secret 或私有配置。
- `run/**`、`QuantProject/**` 的业务逻辑只在明确任务范围内修改。

## Retired Workflow Note

不再强制 Claude Code 计划、Codex 执行、Claude Code 验收。旧 CC-CX 强编排、Guard v4、break-glass 和状态机名 `NORMAL`、`CX_DEGRADED`、`CC_BG_READ`、`CC_BG_WRITE` 只作为历史术语保留，不作为当前主流程要求。

## Ultraplan 预留

后续 Ultraplan 可作为复杂任务计划入口和浏览器审查辅助。小任务仍由 Claude Code 或 Codex 独立完成。
