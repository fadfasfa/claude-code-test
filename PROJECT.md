# claudecode Project Map

`claudecode` 是个人总编程仓、多子项目母仓和本机 agent 执行仓。仓库根目录承载规则、路由、工作流文档和工具脚本，不承载默认业务实现。

Claude Code 与 Codex 均可独立工作；仓库不再维护固定的 CC-CX 强分工主流程。

## Canonical Entries

| 文件 | 用途 |
| :--- | :--- |
| `AGENTS.md` | Codex 当前规则和边界 |
| `docs/archive/superpowers-project-bridge.md` | 旧 bridge 的归档说明 |
| `README.md` | 人类快速入口 |
| `CLAUDE.md` | Claude Code 入口 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/workflows/independent-agent-workflow.md` | Claude Code / Codex 独立工作流 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/07-high-risk-safety.md` | 高危操作确认规则 |
| `docs/workflows/ultraplan-adoption-note.md` | Ultraplan 后续接入说明 |
| `docs/workflows/repository-layout.md` | 目录职责 |
| `docs/workflows/agent-skill-inventory.md` | skill inventory |

## Work Areas

| 路径 | 定位 |
| :--- | :--- |
| `run/` | Hextech 业务运行区 |
| `sm2-randomizer/` | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | 本地私有量化工作区 |
| `heybox/` | 本地抓取脚本 |
| `qm-run-demo/` | demo / runtime 变体 |
| `subtitle_extractor/` | 字幕提取工具 |
| `docs/` / `scripts/` / `.agents/skills/` | 仓库治理区 |
| `.state/workflow/` | 旧 CC-CX 工作流遗留运行态，默认不提交 |
| `.state/cc-work/` | 本机 agent 草稿区，默认不提交 |

业务写入前必须先选定 `target_work_area`。普通任务只产出目标 diff 和对话摘要。Claude Code 中即使 OpenAI Codex plugin 可用，也不得在无用户当前轮显性命令时调用、委派、审查或触发 Codex / CX。旧 CC-CX 执行脚本只允许作为 legacy/compat 线索，不作为主流程要求。

## Agent Workflow

- Claude Code 入口：先 `git status --short`，按 `CLAUDE.md`、`AGENTS.md` 和任务上下文独立完成任务。
- Codex 入口：先 `git status --short`，按 `AGENTS.md`、`PROJECT.md` 和 `docs/index.md` 独立完成任务。
- `S/M/L` 只作为治理边界；本文件不再把 bridge 当作流程入口或权威流程。
- 当前轮已经明确授权或计划已批准的动作，不再重复要求业务层确认；agent 按授权范围执行并验证。
- 两个入口都必须避免混入非本轮脏树，修改后运行最小有效验证。
- 验证通过后，按本轮授权只暂存本轮修改文件并 commit；禁止 `git add .`。push、PR、merge 或 discard 未获明确授权时禁止主动执行；用户明确要求后由 agent 自行完成并验证结果，不要求用户手动输入命令。
- 后续若接入 Ultraplan，只作为复杂任务计划入口；小任务仍由 Claude Code 或 Codex 独立完成。

## Non Goals

- 不用普通仓库任务修改全局工具配置。
- 不把子项目业务规则写入仓库根规则。
- 不恢复 CC-CX 强编排、command、hook、memory、learning、自动 PR shipping、task resume 或高权限 worktree 能力。
