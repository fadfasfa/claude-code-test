# 工作区注册表

本文件记录仓库工作区、子项目类型和默认写入边界。

Codex 是当前唯一主流程。Claude Code 只保留白板和必要接口。

## 默认规则

- 默认可读范围：整个仓库，排除敏感文件。
- 默认可写范围：选定目标工作区的目录树。
- 仓库根目录不是默认业务写入区。
- 新工作区必须先通过 `repo-module-admission`，再在这里登记，最后开始实现。
- 新增工作区本身不构成新增项目级 `.codex/config.toml`、MCP 配置或工具设置的理由。
- 仓库根目录只允许治理 / 控制面文件写入，例如本注册表、入口文件、workflow 文档和工具脚本。

## 工作区选择

1. 实现前声明 `target_work_area`。
2. 声明 `allowed_write_scope`；默认等于目标工作区的 `default_write_scope`。
3. 全仓读取只用于上下文，不读取敏感文件。
4. 目标不清时保持只读，列出候选工作区。
5. Git worktree 是执行面，不自动成为活动工作区。

## 已登记工作区

| work_area | 类型 | 用途 | default_write_scope | read_scope | status | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `run/` | 业务项目 / 数据区域 | Hextech 主运行时、处理、抓取、展示和打包资产 | `run/**` | whole repo | active | 原始数据、不可重建资产和当前脏树默认受保护 |
| `heybox/` | 业务项目 / 爬虫工具 | Heybox 抓取脚本和相关本地文档 / 数据 | `heybox/**` | whole repo | active | 小型独立抓取工作区 |
| `qm-run-demo/` | 实验区 / demo | 带自身 `run/`、文档和元数据的 demo / runtime 变体 | `qm-run-demo/**` | whole repo | active | 写入限制在顶层 demo 树内 |
| `QuantProject/` | 业务项目 / 数据区域 | 量化策略、数据同步、报告和持仓历史 | `QuantProject/**` | whole repo | active | 独立数据 / 模型工作区，业务规则不写入根规则 |
| `sm2-randomizer/` | 业务项目 | Space Marine 2 随机器应用、管线、文档和 debug 资产 | `sm2-randomizer/**` | whole repo | active | 生成产物留在该树内 |
| `subtitle_extractor/` | 工具区 | 字幕提取脚本、依赖和本地项目文档 | `subtitle_extractor/**` | whole repo | active | 独立媒体处理工作区 |
| `docs/` | 治理区 | 仓库工作流、安全、路由和验证规则 | `docs/**` | whole repo | active | 不写子项目业务细节 |
| `scripts/` | 工具区 | 仓库级 Git 和 workflow 辅助脚本 | `scripts/**` | whole repo | active | 脚本必须默认可 dry-run 或只读验证 |
| `.agents/skills/` | 治理区 | 仓库级 Codex skill 白名单和桥接 skill | `.agents/skills/**` | whole repo | active | 不存放业务实现 |

## 操作说明

- 从仓库根目录开始时，默认只做只读探索或仓库治理。
- 不要在仓库根目录创建抓取目录、输出目录、MCP 目录或业务文件，除非任务明确是仓库治理。
- Desktop / OneDrive 支撑文档不是默认参考，除非用户要求工具审计、对比或工作流同步。
- 任何备份失败都必须立即停止，不得继续执行删除、覆盖、移动或其他破坏性动作。

## 新工作区登记模板

| work_area | 类型 | 用途 | default_write_scope | read_scope | status | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `<path>/` | `业务项目/数据区域/工具区/实验区/治理区` | `<用途>` | `<path>/**` | `whole repo` | `candidate/active` | `<约束、依赖或交接说明>` |
