# claudecode

## What This Repo Is

- 多工作区代码与数据仓；`run/`、`sm2-randomizer/`、`QuantProject/` 等各自独立运行。
- 仓库根默认只做入口阅读、只读探查和仓库治理，不把根目录当业务实现区。

## How It Runs

- 读取顺序：`CLAUDE.md` -> `AGENTS.md` -> `PROJECT.md` -> `work_area_registry.md` -> `agent_tooling_baseline.md`
- 先在 `work_area_registry.md` 选目标工作区，再进入对应目录执行实现或验证。
- `.claude/` 只放项目级 settings、skills 和预留接口，不是仓库入口。

## Things That Bite You

- 从仓库根启动时，不直接做工作区实现；根目录只允许只读探查或仓库治理。
- `.ai_workflow/*`、`.claude/worktrees/*`、旧 `.agents/*`、`archive/**`、`.gitnexus/**` 都按历史残留处理，不作为当前依据。
- 不新增项目级 `.codex/config.toml`、`.mcp.json`、`playwright-mcp/` 或其他 MCP 目录。
- 计划批准后，如果发现自己写多了、写坏了、需要缩回最小改动，应直接自行收口并继续；只有遇到 blocker、权限拦截、跨边界风险或 scope 变化时才暂停汇报。

## Commands

```powershell
Get-Content .\work_area_registry.md
git status --short
```
