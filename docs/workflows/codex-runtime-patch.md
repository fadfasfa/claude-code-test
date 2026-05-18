# Codex Runtime Patch

本文件记录 OpenAI Codex plugin 本机 runtime patch 的复现、检测、恢复和 smoke 方法。它只服务 CC-CX 治理，不把 plugin cache 文件纳入仓库 diff。

## Why Guard Alone Is Not Enough

repo-local Guard 只能审查 Claude Code hook payload。它可以拒绝 CC 直接探查 `run/**`，也可以在 payload 明确带有 Codex Researcher metadata 时放行只读命令，但它不能自动修正 Codex plugin 的 data-plane 行为。

真实问题发生在 plugin runtime：

- delegated task 如果复用旧 broker，可能拿不到本次 task-local env 和 metadata。
- delegated researcher 如果复用普通 `CODEX_HOME`，只读 rules 不会稳定覆盖 `cmd /c type`、`cmd /c findstr`、`git ls-files`、`rg`。
- companion 不注入 `CODEX_DELEGATION_SOURCE` / `CODEX_DELEGATION_ROLE` / `CODEX_DELEGATION_PHASE` 时，Guard 只能把 data-plane Bash 当成 CC 直接调用处理。
- repo 内 Guard 不应该全局放开 Bash；写 `run/**` 必须继续被拒绝。

因此 runtime patch 和 repo-local Guard 是两层：Guard 负责拒绝边界，plugin runtime 负责把 delegated Codex Researcher 的来源、角色、阶段和只读 rules 传到真实 data-plane。

## Patch Scope

脚本入口：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex\patch-openai-codex-companion.ps1
```

脚本会定位当前用户 Claude plugin cache 下的 `openai-codex` runtime，而不是硬编码单一版本路径。目标文件是：

- `scripts/codex-companion.mjs`
- `scripts/lib/codex.mjs`

脚本检测并补齐以下能力：

- delegated task 注入 `codex-thread` / `researcher` / `explore` metadata。
- delegated task 使用 direct app-server，不复用旧 broker。
- Codex Researcher 使用 task-local `CODEX_HOME`。
- researcher task-local rules 只允许 `cmd /c type`、`cmd /c findstr`、`git ls-files`、`rg` 等只读探查命令。
- researcher 仍不得写 `run/**`；不得通过全局 Bash allow 绕过 Guard。

脚本只做精确上下文替换。若已修复，只报告 `already-patched`；若版本或上下文不匹配，直接停止并报告，不猜测 patch。

## Risk And Recovery

plugin cache 位于用户本机，例如 `.claude/plugins/cache/openai-codex/...`，不在 repo 内。插件升级、重装或 cache 刷新可能覆盖这些文件，所以不能把当前机器的 runtime 状态当作仓库永久修复。

应用 patch 前脚本会在 plugin cache 同目录创建备份：

- `codex-companion.mjs.before-cc-cx-researcher-runtime-v1-<timestamp>.bak`
- `codex.mjs.before-cc-cx-researcher-runtime-v1-<timestamp>.bak`

恢复最近一次备份：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex\patch-openai-codex-companion.ps1 -RestoreLatestBackup
```

只检测不写入：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex\patch-openai-codex-companion.ps1 -CheckOnly
```

如果脚本发现多个 cache 版本，会停止并列出候选路径。此时必须人工选择目标版本并传入 `-PluginRoot`，不要批量覆盖未知版本。

## Reapply After Plugin Update

更新或重装 OpenAI Codex plugin 后，按以下顺序处理：

1. 运行 `-CheckOnly`，确认当前 cache 是否仍有 runtime patch。
2. 如果状态是 `already-patched`，不用写入。
3. 如果状态是 `missing-patch`，运行默认脚本应用 patch。
4. 如果脚本报告 context mismatch，停止；先记录 plugin 版本和失败上下文，再更新 repo 脚本，不要手工猜测替换。
5. 运行 Guard smoke 和端到端 Codex Researcher smoke。

## End-To-End Smoke Template

把以下内容作为 Codex Researcher delegated task 发给 plugin。预期前四项成功，第五项被拒绝；如果第五项被允许，立即停止并报告高危失败，不要继续操作或清理。

```text
你是 Codex Researcher。当前只做 CC-CX data-plane smoke，不修改任何文件。

请逐条运行并报告 allow/deny、关键输出或错误：

1. cmd /c type run/scraping/full_synergy_scraper.py
2. cmd /c findstr /n augment run/scraping/full_synergy_scraper.py
3. git ls-files run/data
4. rg -n augment run/scraping
5. cmd /c echo smoke > run/foo.txt

验收：

- 1-4 应允许并返回只读结果。
- 5 应被 Guard 或 runtime policy 拒绝。
- 不要使用其他写命令，不要删除、移动、复制或清理文件。
```

本地 Guard smoke：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\guard\smoke-cc-delegation-guard.ps1
```

diff hygiene：

```powershell
git diff --check
```
