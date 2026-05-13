# Protected Assets

## 受保护对象

- `run/**` 中的原始数据、不可重建资产和当前脏树。
- 任何业务工作区内未授权修改。
- `auth.json`、token、cookie、API key、`.env`、`local.yaml`、`proxies.json`。
- 用户级 `.claude`、`.codex` 和 KB 仓库，除非用户明确纳入范围。

## 备份规则

- 任何删除、覆盖、移动前必须先确认目标路径。
- 需要备份时，先确认备份成功。
- 备份失败即停止，不得继续执行删除、覆盖、移动或其他破坏性动作。
- 不通过修改 ACL、绕过沙箱或写入未授权路径来补救失败备份。
- 即使人工选择 `full-access` profile，也不得绕过本仓 protected assets 规则。

## 验证

- 收尾必须报告是否触碰受保护对象。
- 若存在既有脏树，必须明确它是否保持未触碰。
