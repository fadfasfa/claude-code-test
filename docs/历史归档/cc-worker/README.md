# cc-worker retired bundle

本目录保存退役的 Codex -> Claude Code worker 短调用流程，供后续需要时恢复。

## 当前状态

- 当前不启用本流程。
- Codex 不再主动评估或调用这里归档的 worker。
- 仓库 active 索引不再指向原 `scripts/ai` 入口。
- 本目录只作为历史和恢复材料，不作为默认 workflow 来源。

## 归档内容

- `cc-worker.ps1`：原轻量 wrapper 脚本。
- `codex-cc-lightweight-worker.md`：原 workflow 说明。

## 恢复步骤

1. 先按当前 `AGENTS.md` 与 `docs/参考资料/策略说明/module-admission.md` 评估是否真的需要恢复 worker，并明确目标路径和最小验证；不要恢复已删除的 `repo-module-admission` Skill。
2. 在 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/40-Agent与Skill.md`、`docs/当前规则/50-目录职责.md` 中重新登记目标目录和入口；不得直接恢复退役脚本目录或旧 workflow 文档目录。
3. 再按已登记路径移动 `cc-worker.ps1` 和 `codex-cc-lightweight-worker.md`。
4. 在 `AGENTS.md`、`docs/index.md`、`scripts/README.md` 和 support inventory 中同步 active 入口说明。
5. 运行 `git diff --check`、support health 和恢复后 wrapper 的最小行为验证。
