# Work Area Registry

本文件是仓库工作区和默认写入边界事实源。仓库根目录默认只做治理，不承载业务实现。

## Default Rules

- 默认可读范围：整个仓库，排除敏感文件和用户明确禁止路径。
- 默认可写范围：选定目标工作区的目录树。
- 新工作区先走 `repo-module-admission`，再登记到本文件。
- Git worktree 是执行面，不自动成为活动工作区。

## Work Areas

| work_area | 默认写入范围 | 状态 | 说明 |
| :--- | :--- | :--- | :--- |
| `run/` | `run/**` | active | Hextech 运行区；raw data、不可重建资产和当前脏树默认受保护 |
| `sm2-randomizer/` | `sm2-randomizer/**` | active | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | `QuantProject/**` | local-private | 本地私有工作区；默认不发布到 public remote |
| `heybox/` | `heybox/**` | active | 本地抓取脚本 |
| `qm-run-demo/` | `qm-run-demo/**` | active | demo / runtime 变体 |
| `subtitle_extractor/` | `subtitle_extractor/**` | active | 字幕提取工作区 |
| `docs/` | `docs/**` | active | 治理文档、workflow 和历史归档 |
| `scripts/` | `scripts/**` | active | `scripts/workflow/` 是当前入口；`scripts/git/` 是 legacy/manual |
| `.agents/skills/` | `.agents/skills/**` | active | 仓库级 Codex skill 白名单 |
| `.state/workflow/` | `.state/workflow/reports/**` | local-state | 运行态默认 ignored；普通任务不提交 |

## Write Selection

1. 实现前声明 `target_work_area`。
2. `allowed_write_scope` 默认等于目标工作区写入范围。
3. 目标不清时保持只读，列出候选工作区。
4. 任何备份失败都必须立即停止。
