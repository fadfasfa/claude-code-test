# Codex Execution Boundary

本文件只记录 Codex 当前执行边界，不保存账号、密钥、proxy 配置或旧验收细节。

## Execution Surfaces

- Codex-led standalone mode：用户直接调用 Codex 时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务独立执行普通代码任务完整流程。
- CC-led supervised mode：CC 负责理解目标、收敛计划、监督过程、审查 diff 和验收结果；CX / Codex 负责复杂探查、实现、运行验证和第二意见。
- Codex App、VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不混写为同一入口。
- 重执行、长线程和大 diff 默认留在 VS Code Codex / Codex CLI；不要把这类执行历史重新带回 Codex App 热路径。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin。

## Current CX Contract

- 旧根入口 `cx-exec.ps1` 和旧 executor `scripts/workflow/` 已移除。
- 不再维护 `cx-exec` 作为 fallback 或 legacy 主路。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。
- Codex standalone 能力保留；用户直接调用 Codex 时不经过 CC-CX 委派层。
- `.claude/settings.json` 已启用 Codex plugin；plugin 启用不等于 review gate 启用。
- Codex plugin review gate 默认禁用；除非用户显性要求，不得启用 review gate。

## Claude Code Permission Baseline

- Claude Code 项目设置仍保留全局 `Read` allow、`acceptEdits` 和 `Bash(*)` 基线，以兼容 Codex plugin 与本机草稿流。
- 实际 CC 权限由 `.claude/settings.json` 注册的 `PreToolUse` Guard v3 收紧：CC 直接可读可写路径仅限 `.claude/plans/**` 和 `.state/cc-work/**`。
- protected path 的探查、执行、修改和最小验证必须交给 Codex；CC 只保留 intake、plan approval、diff/review surface。
- 本仓外写入、非沙箱以及高风险、不可逆或发布类 Bash 命令仍应提示。

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`、`.codex-exec-apple/`、`.learnings/` 或根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。
- 不把 Codex-led standalone mode 改写成必须经过 CC 或旧 `cx-exec`。
- 不把已启用的 Codex plugin 默认主路误写成“未安装”或“非默认”。
- 不启用 Codex plugin review gate，除非用户显性要求。
- 不在没有用户显性授权非沙箱 CX 时使用 `-Sandbox danger-full-access`。
- 不把非沙箱 CX 授权混同为 CC 直接修改授权。

## Claude Bash Risk Guard

`.claude/settings.json` 注册的 Claude Code `PreToolUse` Guard v3 负责三类动作：

- deny：CC 对 protected path 的 `Read` / `Glob` / `Grep` / `LS` / `Edit` / `Write` / `MultiEdit` / Bash 探查或执行。
- allow：`.claude/plans/**`、`.state/cc-work/**`，以及显式 allowlist 的 Codex control-plane 命令。
- ask：`git reset --hard`、`git clean -fd`、`git checkout -- <path>`、`git commit`、`git push` 等高风险 Git / destructive 操作。

Guard 还要求 `.claude/settings.json` 与 `.claude/hooks/cc-delegation-guard.ps1` 只能在独立治理任务中修改，不得混入业务修复。

## Related

- `10-cc-cx-orchestration.md`
- `repository-layout.md`
- `worktree-policy.md`
