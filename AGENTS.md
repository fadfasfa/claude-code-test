# claudecode Agent Rules

`claudecode` 是个人总编程仓、多子项目母仓和本机 agent 执行仓。Claude Code 与 Codex 均可按各自入口独立工作；任何一方都不是另一方的必经调度层。

## Default

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 每次任务开始先运行 `git status --short`；若已有非本轮修改，先报告并避免混入。
- 只读探查可以直接执行；凡涉及非只读探查、非平凡文件修改、workflow/config/skill/hook 修改、git 写操作、worktree 操作或破坏性命令，必须先输出计划并等待用户确认。
- 普通极小单文件修改若不涉及 workflow/config/skill/hook、git 写、worktree 或破坏性操作，且用户当前轮明确要求直接执行，可以跳过计划确认；仍需按授权范围小步修改并验证。
- 计划必须包含：`git status`、预计修改文件、修改内容、不修改范围、验证命令、Git 处理方式；确认后按计划小步执行，范围变化时重新确认。
- 仓库根目录是治理、路由和工具骨架，不是默认业务写入面。
- 业务修改必须先落到明确子项目或已登记工作区；写入范围以 `docs/workflows/work_area_registry.md` 为准。
- Claude Code 入口按 `CLAUDE.md`、`PROJECT.md` 和 `docs/index.md` 独立执行；Codex 入口按本文件、`PROJECT.md` 和 `docs/index.md` 独立执行。
- 不强制 CC 计划、CX 执行、CC 验收；Claude Code 中即使 OpenAI Codex plugin 可用，也只有用户当前轮显性点名或给出命令时才可调用。
- 旧 CC-CX 强编排和受保护路径编排已退役，不作为日常工作流规则。
- Windows 默认使用 PowerShell。
- 默认在当前工作树小步执行；不自动创建 worktree、分支、计划文件、Markdown report、probe 或 archive 证据文件。
- 修改后运行最小有效验证；无法验证时说明具体原因。

## Canonical Docs

- 文档入口：`docs/index.md`
- workflow 总览：`docs/workflows/00-overview.md`
- 独立 agent 工作流：`docs/workflows/independent-agent-workflow.md`
- Codex 执行边界：`docs/workflows/codex-execution-boundary.md`
- 工作区边界：`docs/workflows/work_area_registry.md`
- 高危操作边界：`docs/workflows/07-high-risk-safety.md`
- 目录职责：`docs/workflows/repository-layout.md`
- worktree 策略：`docs/workflows/worktree-policy.md`
- Ultraplan 预留说明：`docs/workflows/ultraplan-adoption-note.md`
- skill inventory：`docs/workflows/agent-skill-inventory.md`

## Git And Safety

- 只读 Git 命令默认允许，尤其是 `git status --short`、`git diff` 和 `git log`。
- git 写操作必须先进入计划确认，不得把只读 Git 探查扩展为 staging、commit、branch、worktree 或其他写操作。
- 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才 commit。
- commit 前只允许 `git add` 本轮修改文件，禁止 `git add .`。
- 禁止 `git push`，除非用户明确单独要求。
- 禁止 `git reset --hard`、`git clean -fdx`、大范围删除、批量移动或不可逆清理，除非用户明确批准。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、私有配置、`.env`、`auth.json`、`local.yaml`、`proxies.json`。
- 任何备份失败都必须立即停止，不继续删除、覆盖、移动或其他破坏性动作。

## Skills

- 非琐碎代码、脚本、配置或 workflow 实现任务必须触发 `karpathy-project-bridge`。
- 前端 UI / 视觉 / 交互任务还必须触发 `frontend-design-project-bridge`。
- 完成前默认使用 `repo-verification-before-completion` 口径报告证据。
- `.agents/skills/README.md` 是仓库级 Codex skill 白名单入口。
- 不恢复 command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。

## Retired Workflow Note

旧 CC-CX 状态机、Guard v4、break-glass 流程、`NORMAL`、`CX_DEGRADED`、`CC_BG_READ`、`CC_BG_WRITE` 与 protected path 只作为历史术语保留，不作为当前主流程要求。

## Completion Report

非琐碎任务收尾说明：修改文件、是否触碰 `run/**`、是否执行删除/清理/移动、是否 staging/commit/push、diff 摘要、验证命令与结果，以及 acceptance gate。
