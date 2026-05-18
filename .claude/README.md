# Claude Code 本地接口

本目录只服务 Claude Code，不作为当前规则真相源。仓库规则以根目录 `AGENTS.md`、`PROJECT.md`、`CLAUDE.md` 和 `docs/workflows/cc-cx-delegation.md` 为准。

- `skills/`：Claude Code 专用的最小辅助 skill。
- `settings.json`：本仓 Claude Code `PreToolUse` Guard 注册点。Guard v4 默认启用 CX-first 与 CC break-glass 状态机。
- `hooks/cc-delegation-guard.ps1`：仓库级委派 Guard。职责是拒绝 CC 直接探查或修改 protected path，显式放行 OpenAI Codex plugin control-plane，并在明确 Codex Researcher data-plane metadata 时放行 protected path 只读探查。
- `worktrees/`：本地占位目录；不自动创建或主控 Git worktree。
- CC 计划、协作、交接和审查草稿写入 `.state/cc-work/**`。
- Claude Code 原生运行时若需要写入本地计划文件，允许直接使用 `.claude/plans/**`；该目录是本机草稿面，默认不提交。
- NORMAL 状态下，CC 不得直接 `Read` / `Grep` / `LS` / `Edit` / `Write` 业务 protected path，也不得通过 Bash 直接探查、执行、删除、移动、写入或批量格式化 protected path；这些动作必须委派给 Codex。
- CC 可直接读取 `CLAUDE.md`、`AGENTS.md`、`PROJECT.md` 和 `docs/workflows/**` 以完成控制面审查；写入仍受 Guard 治理。
- Guard 状态源优先为 `.state/cc-work/cc-cx-state.json`。缺失时为 `NORMAL`；`CX_DEGRADED` 禁止继续启动或恢复 Codex 执行线程；`CC_BG_READ` 只放行本会话只读工具；`CC_BG_WRITE` 只放行当前 approved plan 的 `approved_files`。
- Codex control-plane allowlist 当前包括：
  - `node .../codex-companion.mjs task ...`
  - `node .../codex-companion.mjs status`
  - `node .../codex-companion.mjs cancel ...`
  - `node .../codex-companion.mjs resume ...`
  - `node .../codex-companion.mjs task-resume-candidate ...`
  - `codex task ...`
  - `codex cancel ...`
  - `codex resume ...`
  - `codex status`
  - `codex review`
- Codex data-plane 不是 control-plane 的隐式延伸；读取 protected path 必须由 companion 向 hook payload 注入 `codex_delegation.source=codex-thread`，并标明 `role=researcher` 或 `phase=explore`。
- Codex Researcher data-plane 只允许 `Get-Content` / `type` / `cmd /c type` / `findstr` / `Select-String` / `rg` / `grep` / `Get-ChildItem` / `dir` / `ls` / `git status` / `git diff` / `git log` / `git ls-files` 这类只读命令；重定向、写文件、删除/移动/复制和高危 Git 写操作仍必须拒绝。
- `.claude/settings.json` 与 `.claude/hooks/cc-delegation-guard.ps1` 属于 Guard 治理面。业务任务不得修改；如需修改，必须单列为独立治理任务并按 approved plan 执行。
- Guard smoke test 运行方法：
  - `pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\guard\smoke-cc-delegation-guard.ps1`
- Guard smoke 使用临时 state path 并关闭默认 debug log，不应写入 `.state/cc-work/guard-payload-debug.jsonl`。
- OpenAI Codex plugin runtime cache patch 的检测、复现、恢复和端到端 smoke 见 `docs/workflows/codex-runtime-patch.md`；不要把 `.claude/plugins/cache/**` 当作 repo 持久修复。
- 不在这里保存 Codex 配置、CC -> CX 运行态、长期报告或仓库级 workflow 规则。
