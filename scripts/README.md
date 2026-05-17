# scripts

本目录放仓库级辅助脚本。默认入口是 `scripts/workflow/`；`scripts/git/` 是 legacy/manual 层，不自动触发。

| 路径 | 状态 | 用途 |
| :--- | :--- | :--- |
| `scripts/workflow/` | current | 当前 workflow 脚本和测试 |
| `scripts/git/` | legacy/manual | 旧 Git / worktree 辅助脚本，只能显式手动调用 |
| `scripts/archive/` | archive | 退役或一次性脚本；可不存在 |

修改脚本时必须说明用途、输入输出、写入行为和失败行为。不要在脚本中读取凭据文件或默认执行发布动作。
