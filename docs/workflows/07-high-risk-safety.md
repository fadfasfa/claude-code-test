# High-Risk Safety

本文件记录高危资产和高危操作确认规则，不定义日常编排状态机。

## 高危资产

- `run/**` 中的原始数据、不可重建资产和当前脏树。
- `QuantProject/**` 本地私有工作区，默认不发布到 public remote。
- 任何业务工作区内未授权修改。
- `auth.json`、token、cookie、API key、`.env`、`local.yaml`、`proxies.json`。
- 用户级 `.claude`、`.codex` 和 KB 仓库，除非用户明确纳入范围。

## 高危操作

- `git push`、PR、merge、amend、tag、release。
- `git reset --hard`、`git clean -fdx`、批量删除、批量移动、不可逆清理。
- 跨工作区重构、批量格式化、迁移生成物或删除运行态目录。
- 修改凭据、登录态、proxy、账号池或全局配置。

## 执行规则

- 删除、覆盖、移动前必须先确认目标路径。
- 需要备份时，先确认备份成功；备份失败即停止。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。
- 不通过修改 ACL、绕过沙箱或写入未授权路径来补救失败备份。
- 即使人工选择高权限 profile，也不得绕过本仓高危资产规则。

## 收尾报告

- 说明是否触碰 `run/**`、`QuantProject/**` 或其他业务工作区。
- 说明是否执行删除、清理、移动、staging、commit 或 push。
- 若存在既有脏树，明确它是否保持未触碰。
