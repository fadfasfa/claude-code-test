# Codex Execution Boundary

本文件只记录 Codex 当前执行边界，不保存账号、密钥、proxy 配置或旧验收细节。

## Execution Surfaces

- Codex-led standalone mode：用户直接调用 Codex 时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务独立执行普通代码任务完整流程。
- CC-led supervised mode：CC 负责理解目标、收敛计划、监督过程、审查 diff 和验收结果；CX / Codex 负责复杂探查、实现、运行验证和第二意见。
- Codex App、VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不混写为同一入口。
- 重执行、长线程和大 diff 默认留在 VS Code Codex / Codex CLI；不要把这类执行历史重新带回 Codex App 热路径。
- CC 需要调用 CX 时，默认主路是已启用的 OpenAI 官方 Codex plugin。
- plugin runtime cache 是本机状态；Codex Researcher data-plane patch 的检测、复现和恢复见 `codex-runtime-patch.md`。

## Current CX Contract

- 旧根入口 `cx-exec.ps1` 和旧 executor `scripts/workflow/` 已移除；如在历史路径、缓存或兼容层出现，只能视为 legacy/compat。
- 不再维护 `cx-exec` 作为 CC-CX 主流程、fallback 主路或验收接口。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。
- Codex standalone 能力保留；用户直接调用 Codex 时不经过 CC-CX 委派层。
- `.claude/settings.json` 已启用 Codex plugin；plugin 启用不等于 review gate 启用。
- Codex plugin review gate 默认禁用；除非用户显性要求，不得启用 review gate。

## Claude Code Permission Baseline

- Claude Code 项目设置仍保留全局 `Read` allow、`acceptEdits` 和 `Bash(*)` 基线，以兼容 Codex plugin 与本机草稿流。
- 实际 CC 权限由 `.claude/settings.json` 注册的 `PreToolUse` Guard v4 收紧：NORMAL 状态下 CC 直接写入仅限 `.claude/plans/**` 和 `.state/cc-work/**`，可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 以完成控制面审查。
- protected path 的探查、执行、修改和最小验证默认必须交给 Codex；CC 只保留 intake、plan approval、diff/review surface。用户授权后可通过 `CC_BG_READ` 或 `CC_BG_WRITE` break-glass。
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

`.claude/settings.json` 注册的 Claude Code `PreToolUse` Guard v4 负责以下动作：

- state：优先读取 `.state/cc-work/cc-cx-state.json`，支持 `NORMAL`、`CX_DEGRADED`、`CC_BG_READ`、`CC_BG_WRITE`。
- deny：NORMAL/CX_DEGRADED 下 CC 对业务 protected path 的 `Read` / `Glob` / `Grep` / `LS` / `Edit` / `Write` / `MultiEdit` / Bash 探查或执行。
- allow：`.claude/plans/**`、`.state/cc-work/**`、控制面文档只读，以及显式 allowlist 的 OpenAI Codex plugin control-plane 命令。
- break-glass：`CC_BG_READ` 放行本会话直接只读工具；`CC_BG_WRITE` 只放行 approved plan 的 `approved_files`。
- git：`git add`、`git commit`、`git push` 默认 deny；push 必须单独授权，不能继承 commit 授权。
- prompt safety：Guard 只按 `tool_name + tool_input` 的路径字段判断；`Grep.pattern`、prompt 和 description 中出现 protected path 不触发 deny。

Guard 还要求 `.claude/settings.json` 与 `.claude/hooks/cc-delegation-guard.ps1` 只能在独立治理任务中修改，不得混入业务修复。

## Related

- `10-cc-cx-orchestration.md`
- `codex-runtime-patch.md`
- `repository-layout.md`
- `worktree-policy.md`
