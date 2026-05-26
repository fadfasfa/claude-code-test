# claudecode

`claudecode` 是多工作区本地开发仓库。Claude Code 与 Codex 均可独立工作；仓库只保留轻量规则、工作区边界和高危操作约束。

## Daily Entry

| 路径 | 用途 |
| :--- | :--- |
| `README.md` | 人类快速入口 |
| `PROJECT.md` | agent 仓库地图 |
| `AGENTS.md` | Codex 规则和边界 |
| `CLAUDE.md` | Claude Code 入口 |
| `docs/archive/superpowers-project-bridge.md` | 旧 bridge 归档说明 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/` | 当前 workflow 规则 |
| `scripts/` | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |

## Work Areas

业务工作区包括 `run/`、`sm2-randomizer/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`subtitle_extractor/`。治理区包括 `docs/`、`scripts/`、`.agents/skills/` 和入口文件。具体写入边界以 `docs/workflows/work_area_registry.md` 为准。

## Workflow

- Claude Code 入口直接按 `CLAUDE.md`、`AGENTS.md` 摘要、`PROJECT.md`、`docs/index.md` 和任务上下文执行。
- Codex 入口直接按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和任务上下文执行。
- `S/M/L` 只作为治理边界；官方 Superpowers plugin 是唯一 Superpowers 来源，仓库不再维护 bridge。
- 每次任务先 `git status --short`；修改后运行最小有效验证。
- 验证通过后，按本轮授权只暂存本轮修改文件并提交；禁止 `git add .`。push、PR、merge 或 discard 未获明确授权时禁止主动执行；用户明确要求后由 agent 自行完成并验证结果，不要求用户手动输入命令。
- OpenAI Codex plugin 可以保留启用状态，但 Claude Code 没有用户当前轮显性点名或命令时不得调用、委派、审查或触发 Codex / CX。
- 旧 CC-CX 执行脚本和强编排已移除，`.state/workflow/**` 只作为旧运行态遗留，不再作为新主路验收接口。
- `docs/reference/` 和 `docs/archive/` 默认不整体读取。
- 普通任务不生成计划、Markdown 报告、probe 或 archive 证据文件。
