# scripts

本目录放仓库级辅助脚本。旧 `scripts/workflow/` 主流程已移除；`scripts/git/` 是 legacy/manual 层，不自动触发。

| 路径 | 状态 | 用途 |
| :--- | :--- | :--- |
| `scripts/git/` | legacy/manual | 旧 Git / worktree 查看与清理辅助脚本，只能显式手动调用 |

不维护新的复杂编排器。新增脚本必须有明确用途、输入输出、写入行为和失败行为；不要读取凭据文件或默认执行发布动作。
