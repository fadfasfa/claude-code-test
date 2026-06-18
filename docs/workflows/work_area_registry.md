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
| `sms-monitor/` | `sms-monitor/**` | active | 本地 SMS 接码验证码监控工具 |
| `qm-run-demo/` | `qm-run-demo/**` | active | demo / runtime 变体 |
| `subtitle_extractor/` | `subtitle_extractor/**` | active | 字幕提取工作区 |
| `docs/` | `docs/**` | active | 治理文档、workflow 和历史归档 |
| `scripts/` | `scripts/**` | active | 仓库级辅助脚本；旧 `scripts/workflow/` 已移除，`scripts/git/` 是 legacy/manual |
| `.agents/skills/` | `.agents/skills/**` | active | 仓库级 Codex skill 白名单 |
| `.claude/commands/` | `.claude/commands/**` | active | Claude Code 项目级 slash command；只允许薄入口，不承载 hook 或清理实现 |
| `.claude/skills/` | `.claude/skills/**` | active | Claude Code 项目级最小 skill；不作为 command、hook 或 Codex skill 白名单 |
| `.state/workflow/` | 不作为新任务写入面 | legacy-state | 旧 CC-CX 工作流遗留运行态；新主路不依赖旧任务结果文件 |
| `.state/cc-work/` | `.state/cc-work/**` | local-state | 本机 agent 计划、协作、交接、审查草稿；不是正式文档区 |

## Write Selection

1. 实现前声明 `target_work_area`。
2. `allowed_write_scope` 默认等于目标工作区写入范围。
3. 目标不清时保持只读，列出候选工作区。
4. 任何备份失败都必须立即停止。
