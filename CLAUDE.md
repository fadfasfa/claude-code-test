# claudecode

`claudecode` 是一个多工作区开发仓库。它包含仓库治理文件，也包含 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/` 等独立 coding / data 工作区。

仓库根目录是工作流入口和任务路由层，不是默认业务实现目录。

## Claude Code 读取顺序

1. `CLAUDE.md`
2. `AGENTS.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. 相关 `docs/*.md`

入口文件不是每次都要全量读取。普通任务优先读取中文头部简介、目录、目标工作区相关段落和相关小节；已知 `target_work_area` 时，只搜索该工作区相关段落。不要默认 `Read` `AGENTS.md`、`PROJECT.md` 或 `work_area_registry.md` 的 1-2000 行，也不要把完整入口链全文件读取当作启动固定动作。`Read` 因参数、行号或范围失败一次后，不得用相同参数重试，应改用 `rg`、`Select-String`、`Get-Content -TotalCount` 或小范围行读取。

`.claude/` 存放 repo-local settings、skills、hooks 和 tools。它不能替代根目录入口文件。

## 默认流程

- 小任务：确认目标工作区，读取窄范围上下文，修改，运行最近验证，报告。
- 中任务：写简短计划，必要时确认假设，在窄范围内分步修改，验证，报告。
- 大任务：收敛需求，拆分工作，判断是否需要 worktree，判断是否需要 TDD，可选使用 subagents 做边界清晰的旁路工作，最后做 PR-style review。
- 对大任务，进入详细计划前可先做轻量 brainstorm / 方案比较，用于收敛方向；brainstorm 本身不得替代验收计划、任务拆分或验证。

详细决策表见 `docs/task-routing.md`。

## 边界

- 本仓不继承 `kb` 的 ingest/wiki 工作流。
- 不得从本仓工作流修改 `C:\Users\apple\kb`。
- 不得从本仓工作流修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS 插件、Codex App、Codex Proxy、全局 hooks、全局 skills 或全局 AGENTS/CLAUDE 文件。
- 不要创建项目级 `.codex/config.toml`、`.mcp.json`、`playwright-mcp/` 或其他 MCP 目录。
- 不启用完整 Superpowers SessionStart，也不启用 ECC。

## 工作区规则

业务实现前，使用 `work_area_registry.md` 声明 `target_work_area`。

如果任务只修改仓库治理文件，目标工作区是 `repo-root-governance`。如果目标不清楚，保持只读并列出候选项。

## 连续执行

对已接受的多步计划，只把 `.tmp/active-task/current.md` 当作运行态 ledger。它已被 ignored，不是 learning，不是规则来源，也不能授权危险操作。

Plan Mode 审批通过 ExitPlanMode 或对话提交完成；不要默认把计划写到 `C:\Users\apple\.claude\plans\*.md`。如确实需要计划落盘，只能写 repo 内 `.tmp/active-task/current.md`；全局 `.claude\plans` 写入必须由用户显式要求。

已批准范围内的安全步骤可以继续执行。遇到 blocker、范围变化、dirty-tree 范围不清、危险 git 操作、global/kb 边界风险、依赖安装或用户意图不清时，必须停下。

## 常用入口文档

```powershell
Select-String .\work_area_registry.md -Pattern '<target_work_area>'
Get-Content .\docs\task-routing.md -TotalCount 120
Get-Content .\docs\safety-boundaries.md -TotalCount 120
Get-Content .\docs\continuous-execution.md -TotalCount 120
git status --short --branch
```
