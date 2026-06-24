# 模块准入

新增或扩展仓库级 workflow 模块、skill、hook、tool、验证脚本、Playwright 配置或其他长期自动化前，先读本文件。

## 规则

不要因为方便就新增仓库级能力。只有当问题重复、仓库特有、无法由 `AGENTS.md`、普通 Codex 能力或短一次性命令解决时，才允许进入准入判断。

不新增 memory、learning promotion、长期上下文晋升、PR shipping、task resume 或 worktree governance skill。

## 准入卡

- 名称：
- 类型：
- 解决什么问题：
- 不解决什么问题：
- 触发条件：
- 读取路径：
- 写入路径：
- 依赖影响：
- 浏览器影响：
- Git / 全局 / KB 影响：
- 停用路径：
- 删除路径：
- 最小验证：
- 为什么现有规则或 Codex 能力不够：
- 状态：

## 当前已准入能力

### repo-module-admission

- 类型：仓库级 Codex skill。
- 用途：新增长期仓库级能力前要求显式准入判断。
- 入口：`.agents/skills/repo-module-admission/SKILL.md`。
- 状态：active。

### repo-verification-before-completion

- 类型：仓库级 Codex skill。
- 用途：报告完成前要求给出窄范围验证证据。
- 入口：`.agents/skills/repo-verification-before-completion/SKILL.md`。
- 状态：active。

### Playwright 任务级验证

- 类型：可选任务级前端验证。
- 用途：前端任务需要浏览器或截图检查时使用。
- 边界：不经单独准入卡和用户确认，不安装依赖、不新增配置、不新增脚本、不写 hook。
- 状态：仅在任务需要时可用。
