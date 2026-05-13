# Model Routing

## 默认路由

- Codex 是本仓当前唯一主执行入口。
- Claude Code 只保留白板和必要接口，不作为本仓治理面。
- 本仓不设置 repo-local `.codex/config.toml`。
- 权限配置走全局默认：低摩擦 `granular` + `rules/default.rules` 高危闸门。
- `full-access` profile 只能人工选择，不是本仓默认执行面。

## Skill 路由

- 代码任务触发 `karpathy-project-bridge` 和全局 `karpathy-guidelines`。
- 前端 UI 任务触发 `frontend-design-project-bridge` 和全局 `frontend-design`，同时仍触发 `karpathy-project-bridge`。
- Superpowers 只作为能力索引，不覆盖仓库规则。

## 禁止

- 不在本仓写入真实 API key、token、cookie 或 proxy secret。
- 不用子项目任务修改全局模型、代理或账户配置。
- 不为本仓恢复 repo-local Codex config。
