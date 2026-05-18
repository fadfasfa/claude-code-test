# Claude Code 本地接口

本目录只服务 Claude Code，不作为当前规则真相源。仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md` 和 `docs/workflows/cc-cx-delegation.md` 为准。

- `skills/`：Claude Code 专用的最小辅助 skill。
- `settings.json`：本仓 Claude Code `PreToolUse` Guard 注册点。Guard v3 默认启用严格 CC/CX 分工。
- `hooks/cc-delegation-guard.ps1`：仓库级委派 Guard。职责是拒绝 CC 直接探查或修改 protected path，并显式放行 Codex control-plane。
- `worktrees/`：本地占位目录；不自动创建或主控 Git worktree。
- CC 计划、协作、交接和审查草稿写入 `.state/cc-work/**`。
- Claude Code 原生运行时若需要写入本地计划文件，允许直接使用 `.claude/plans/**`；该目录是本机草稿面，默认不提交。
- 业务任务中，CC 不得直接 `Read` / `Grep` / `LS` / `Edit` / `Write` protected path，也不得通过 Bash 直接探查、执行或修改 protected path；这些动作必须委派给 Codex。
- Codex control-plane allowlist 当前包括：
  - `node .../codex-companion.mjs task ...`
  - `node .../codex-companion.mjs status`
  - `node .../codex-companion.mjs cancel ...`
  - `node .../codex-companion.mjs task-resume-candidate ...`
  - `codex resume ...`
  - `codex status`
  - `codex review`
- `.claude/settings.json` 与 `.claude/hooks/cc-delegation-guard.ps1` 属于 Guard 治理面。业务任务不得修改；如需修改，必须单列为独立治理任务并由 Codex 执行。
- Guard smoke test 运行方法：
  - `pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\guard\smoke-cc-delegation-guard.ps1`
- 不在这里保存 Codex 配置、CC -> CX 运行态、长期报告或仓库级 workflow 规则。
