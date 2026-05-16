# scripts 目录说明

本目录放仓库级辅助脚本。默认优先使用 `scripts/workflow/` 下的当前 workflow 脚本；`scripts/git/` 是 legacy compatibility，只能手动调用，不作为自动 worktree 主控。

| 路径 | 作用 | 是否会修改状态 |
| :--- | :--- | :--- |
| `scripts/workflow/` | 当前工作流入口：verify、local review、worktree、CC -> CX executor | 视具体参数；多数默认 dry-run 或只读 |
| `scripts/git/` | 旧 Git / worktree 辅助脚本 | 可能创建或清理 worktree，必须显式手动调用 |

修改脚本时必须说明用途、输入输出、关键路径、是否写文件，以及失败时行为。
