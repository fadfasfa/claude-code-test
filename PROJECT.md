# claudecode Project Map

`claudecode` 是个人总编程仓、多子项目母仓和 Codex 主执行仓。仓库根目录承载规则、路由、工作流文档和工具脚本，不承载默认业务实现。

Codex 是当前唯一主流程。Claude Code 只保留入口和边界说明。

## Canonical Entries

| 文件 | 用途 |
| :--- | :--- |
| `AGENTS.md` | Codex 当前规则和边界 |
| `README.md` | 人类快速入口 |
| `CLAUDE.md` | Claude Code 入口 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界 |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/10-cc-cx-orchestration.md` | CC/CX 契约 |
| `docs/workflows/cc-cx-delegation.md` | CC/CX 分工、阶段和故障恢复 |
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
| `.state/workflow/` | 旧 `cx-exec` 工作流遗留运行态，默认不提交 |
| `.state/cc-work/` | CC 计划、协作、交接、审查草稿区 |

业务写入前必须先选定 `target_work_area`。普通任务只产出目标 diff 和对话摘要。CC 需要调用 CX 时，主路是 OpenAI 官方 Codex plugin；`cx-exec.ps1` 只允许作为 legacy/compat 线索，不作为主流程要求。Codex 被用户直接调用时仍保留 standalone 执行能力。

## CC / CX Boundary

- CC 只负责需求澄清、任务派发、计划审批、结果审查。
- Codex 负责仓库探查、计划证据收集、代码定位、patch 生成、apply 和最小验证。
- NORMAL 状态下，CC 直接可写路径仅限 `.claude/plans/**` 和 `.state/cc-work/**`；可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 以完成控制面审查。
- `run/`、`QuantProject/`、`heybox/`、`qm-run-demo/`、`sm2-randomizer/`、`subtitle_extractor/`、根入口文档写入、`docs/workflows/` 写入以及 `.claude/` Guard 治理面都属于 protected path；默认由 Codex 负责探查和修改。
- CC break-glass 由 `.state/cc-work/cc-cx-state.json` 控制：`CC_BG_READ` 只放行本会话只读工具，`CC_BG_WRITE` 只放行 approved plan 的 `approved_files`。
- Guard 或 `.claude/settings.json` 的调整必须作为独立治理任务处理，不得混入业务修复。

## Non Goals

- 不用普通仓库任务修改全局工具配置。
- 不把子项目业务规则写入仓库根规则。
- 不恢复 command、hook、memory、learning、自动 PR shipping、task resume 或高权限 worktree 能力。
