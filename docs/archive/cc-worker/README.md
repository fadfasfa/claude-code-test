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

1. 将 `cc-worker.ps1` 移回 `scripts/ai/cc-worker.ps1`。
2. 将 `codex-cc-lightweight-worker.md` 移回 `docs/workflows/codex-cc-lightweight-worker.md`。
3. 在 `AGENTS.md` 恢复 Codex 主动评估规则。
4. 在 `docs/index.md` 和 `scripts/README.md` 恢复 active 入口说明。
5. 更新 support inventory，把该 worker 从 retired 改回 active。
6. 运行 `git diff --check` 和 support health，再按需要验证 wrapper 行为。
