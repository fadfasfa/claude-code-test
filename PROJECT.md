# claudecode Project Map

`claudecode` 是个人总编程仓、多子项目母仓和 Codex 编程主执行仓。仓库根目录承载规则、路由、工作流文档和工具脚本，不承载默认业务实现。

Codex 是当前唯一主流程。Claude Code 只保留白板和必要接口。

## Canonical Entries

| 文件 | 用途 |
| :--- | :--- |
| `AGENTS.md` | Codex 当前规则和边界 |
| `CLAUDE.md` | Claude Code 项目入口和 CC 边界 |
| `README.md` | 人类快速入口 |
| `docs/index.md` | 文档短索引 |
| `docs/workflows/work_area_registry.md` | 工作区注册表和写入边界 |
| `docs/workflows/00-overview.md` | workflow 总览 |
| `docs/workflows/codex-execution-boundary.md` | Codex 执行边界 |
| `docs/workflows/10-cc-cx-orchestration.md` | CC/CX 契约 |
| `docs/workflows/repository-layout.md` | 目录职责 |
| `docs/workflows/agent-skill-inventory.md` | Codex skill inventory |

## Work Areas

| 路径 | 定位 |
| :--- | :--- |
| `run/` | Hextech 业务运行区，不承载仓库级 workflow 运行态 |
| `sm2-randomizer/` | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | 本地私有量化工作区 |
| `heybox/` | 本地抓取脚本 |
| `qm-run-demo/` | demo / runtime 变体 |
| `subtitle_extractor/` | 字幕提取工具 |
| `docs/` | 治理文档、短索引、reference 和 archive |
| `scripts/` | 仓库级辅助脚本 |
| `.agents/skills/` | 仓库级 Codex skill 白名单 |
| `.state/workflow/` | CC -> CX 本地运行态，默认不提交 |

业务写入前必须从 `docs/workflows/work_area_registry.md` 选择目标工作区。普通 Codex 修改只产出目标 diff 和对话摘要。

## Non Goals

- 不用普通仓库任务修改全局工具配置。
- 不把本仓 workflow 推入 KB 仓库。
- 不把子项目业务规则写入仓库根规则。
- 不恢复 command、hook、memory、learning、自动 PR shipping、task resume 或高权限 worktree 能力。
