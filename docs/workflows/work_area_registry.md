# Work Area Registry

本文件是仓库工作区和默认写入边界事实源。仓库根目录默认只做治理，不承载业务实现。

## Default Rules

- 默认可读范围：整个仓库，排除敏感文件和用户明确禁止路径。
- 默认可写范围：选定目标工作区的目录树。
- 新工作区必须先通过 `repo-module-admission`，再登记到本文件。
- Git worktree 是执行面，不自动成为活动工作区。
- Desktop / OneDrive 支撑文档不是默认参考，除非用户要求工具审计或工作流同步。

## Work Areas

| work_area | 类型 | 默认写入范围 | 状态 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `run/` | 业务项目 / 数据区域 | `run/**` | active | Hextech 运行、处理、抓取、展示和打包资产；raw data、不可重建资产和当前脏树默认受保护 |
| `sm2-randomizer/` | 业务项目 | `sm2-randomizer/**` | active | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | 本地私有工作区 / 数据区域 | `QuantProject/**` | local-private | 不发布到 public remote；默认不被其他任务读取或修改 |
| `heybox/` | 业务项目 / 爬虫工具 | `heybox/**` | active | 本地抓取脚本 |
| `qm-run-demo/` | 实验区 / demo | `qm-run-demo/**` | active | demo / runtime 变体 |
| `subtitle_extractor/` | 工具区 | `subtitle_extractor/**` | active | 字幕提取工作区 |
| `docs/` | 治理区 | `docs/**` | active | 短索引、workflow、安全、路由、验证规则和历史归档 |
| `scripts/` | 工具区 | `scripts/**` | active | `scripts/workflow/` 是当前入口；`scripts/git/` 是 legacy/manual |
| `.agents/skills/` | 治理区 | `.agents/skills/**` | active | 仓库级 Codex skill 白名单和桥接 skill |
| `.state/workflow/` | 本地运行态 | `.state/workflow/reports/**` | local-state | 机器结果和滚动状态默认 ignored；普通任务不提交 |

## Write Selection

1. 实现前声明 `target_work_area`。
2. 声明 `allowed_write_scope`；默认等于目标工作区写入范围。
3. 目标不清时保持只读，列出候选工作区。
4. 任何备份失败都必须停止，不继续删除、覆盖、移动或其他破坏性动作。
