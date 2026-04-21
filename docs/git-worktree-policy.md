# Git Worktree 方案（cc/cx 手工流程）

本仓库采用「手工 Git worktree + 薄包装脚本」方案，原因如下：
- 避免依赖 Claude 的 `WorktreeCreate` / `SessionEnd` hooks 参与生命周期管理。
- 让工作流可回放、可审计、可撤销：任何操作都来自可阅读的本地脚本，不依赖隐式自动化。
- 与仓库内多工作区场景一致：按任务创建临时隔离环境，主树保持清晰。

## 目录约定

### 兄弟目录（sibling worktree root）
默认放在：

- `<repo-parent>\<repo-name>.worktrees\`

示例（当前仓库为 `C:\Users\apple\claudecode`）：

- `C:\Users\apple\claudecode.worktrees\`

### 命名约定

- worktree 目录名：`<type>-<topic>`
- branch：`cc/<type>/<topic>`
- Claude session：`cc-<type>-<topic>`

推荐的 `type`：

- `task`
- `hotfix`
- `review`
- `spike`

### topic 规范

- 自动 slug 化为小写
- 非字母数字字符统一替换为 `-`
- 连续 `-` 折叠
- 去掉首尾 `-`

## 标准流程

1. 创建
   - 使用 `scripts/git/ccw-new.ps1 -Type <type> -Topic <topic>`
2. 进入
   - `cd <worktree path>`
3. 启动 Claude
   - `claude -n <session>`
4. 合并
   - 任务完成后按正常流程合并分支
5. GC
   - 完成回收前执行 `scripts/git/ccw-gc.ps1`

> 说明：脚本不会自动启动 Claude，不会自动装依赖，不会自动复制 `.env` / `.env.local`。

## 使用限制

- 同一本地分支不要在多个 worktree 中占用。
- 每个新 worktree 都要自行初始化依赖（venv / node_modules）与本地工具链。
- 本方案不自动复制本地配置文件（如 `.env` / `.env.local`）。

## 命令示例

### 创建新 worktree
```powershell
.\scripts\git\ccw-new.ps1 -Type task -Topic "retry-cache"
```

### 指定 base 与 root
```powershell
.\scripts\git\ccw-new.ps1 -Type review -Topic "ui-polish" -Base origin/main -Root "D:\wt\claudecode"
```

### 列出仓库 worktree
```powershell
.\scripts\git\ccw-ls.ps1
```

### 查看可清理项（dry-run）
```powershell
.\scripts\git\ccw-gc.ps1
```

### 执行清理
```powershell
.\scripts\git\ccw-gc.ps1 -Apply
```
