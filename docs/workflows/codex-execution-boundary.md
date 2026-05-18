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

- Claude Code 项目设置采用全局 `Read` allow，减少只读探索提示；凭据类文件仍 deny。
- 本仓普通文件编辑通过 `acceptEdits` 和 `Edit(/**)` / `Write(/**)` / `MultiEdit(/**)` 低摩擦执行。
- Bash 默认通过 `Bash(*)` 和 sandbox auto-allow 降低提示；普通仓库内 Bash 不再逐条确认。
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

`.claude/settings.json` 仍注册 Claude Code `PreToolUse` guard，但 guard 已收窄为高风险 Bash 提示层：删除文件、强制清理、回退工作树、重写 Git 历史、提交和推送等操作需要显式确认；普通 `Edit` / `Write` / `MultiEdit` 与普通仓库内 Bash 不再被 repo-local hook 一刀切拦截。本仓外写入或非沙箱执行继续依赖 Claude Code 自身权限确认与 sandbox 边界。

## Related

- `10-cc-cx-orchestration.md`
- `repository-layout.md`
- `worktree-policy.md`
