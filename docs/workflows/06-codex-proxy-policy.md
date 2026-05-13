# Codex Proxy Policy

## 当前原则

- Codex proxy 属于全局配置，不属于本仓业务配置。
- Codex 权限口径也属于全局配置：默认低摩擦 `granular`，高危动作由 `rules/default.rules` 拦截。
- 本仓不恢复 repo-local `.codex/config.toml`。
- 不把 API key、token、cookie、proxy secret 写入仓库。

## 可做

- 记录本仓是否依赖全局 Codex baseline。
- 在工具基线里说明本仓不维护 proxy 真相源。
- 对仓库内误写的 repo-local Codex 配置做删除或阻断。

## 不做

- 不读取或修改 `local.yaml`、`proxies.json`、账户池、真实 key。
- 不在子项目任务中改变全局代理配置。
- 不把 `full-access` profile 写成仓库默认。
