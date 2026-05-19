# Codex Execution Boundary

本文件只记录 Codex 当前独立执行边界，不保存账号、密钥、proxy 配置或旧验收细节。

## Execution Surfaces

- Codex standalone mode：用户直接调用 Codex 时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务独立执行普通代码任务完整流程。
- Codex App、VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不混写为同一入口。
- 重执行、长线程和大 diff 默认留在 VS Code Codex / Codex CLI；不要把这类执行历史重新带回 Codex App 热路径。
- OpenAI Codex plugin 可以作为可选辅助工具；不得写成 Claude Code 或 Codex 的强制主流程。

## Current Contract

- 旧根入口 `cx-exec.ps1` 和旧 executor `scripts/workflow/` 已移除；如在历史路径、缓存或兼容层出现，只能视为 legacy/compat。
- 不再维护 `cx-exec` 作为主流程、fallback 主路或验收接口。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；当前工作流不依赖 `.state/workflow/tasks/result.json`。
- Codex standalone 能力保留；用户直接调用 Codex 时不经过 Claude Code 委派层。
- `.claude/settings.json` 可保留 Codex plugin 启用状态；plugin 启用不等于 review gate 启用。
- Codex plugin review gate 默认禁用；除非用户显性要求，不得启用 review gate。

## Codex Task Rules

- 开始任务先运行 `git status --short`。
- 只读探查可以直接执行；凡涉及非只读探查、非平凡文件修改、workflow/config/skill/hook 修改、git 写操作、worktree 操作或破坏性命令，必须先输出计划并等待用户确认。
- 普通极小单文件修改若不涉及 workflow/config/skill/hook、git 写、worktree 或破坏性操作，且用户当前轮明确要求直接执行，可以跳过计划确认；仍需按授权范围小步修改并验证。
- 计划必须包含：`git status`、预计修改文件、修改内容、不修改范围、验证命令、Git 处理方式；确认后按计划小步执行，范围变化时重新确认。
- 先选择明确工作区，避免把仓库根当作默认业务写入面。
- 修改后运行最小有效验证；无法验证时说明原因。
- 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才只暂存本轮修改文件并 commit。
- 禁止 `git add .` 和默认 push。
- 本仓外写入、非沙箱以及高风险、不可逆或发布类 Bash 命令仍应提示。

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`、`.codex-exec-apple/`、`.learnings/` 或根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。
- 不把 Codex standalone mode 改写成必须经过 Claude Code、Codex plugin 或旧 `cx-exec`。
- 不启用 Codex plugin review gate，除非用户显性要求。
- 不在没有用户显性授权非沙箱 Codex 时使用 `-Sandbox danger-full-access`。
- 不把非沙箱 Codex 授权混同为 Claude Code 修改授权。

## Related

- `independent-agent-workflow.md`
- `repository-layout.md`
- `worktree-policy.md`
