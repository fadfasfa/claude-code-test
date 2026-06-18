# Codex CC Lightweight Worker

本文件定义 Codex App 主动评估、显式短调用 Claude Code CLI / GLM-5.1 的轻量工作流。它不是旧 CC-CX bridge，不创建任务队列、daemon、状态机、hook、数据库、命令拦截或多层调度系统。

## 角色

| 角色 | 职责 |
| :--- | :--- |
| Codex App | 主控端、架构师、任务分级者、最终 reviewer、Git 和发布边界守门人 |
| Claude Code CLI / GLM-5.1 | 高信任受控实现代理、局部工程执行者、方案探索者、反向 reviewer |
| Claude Code VS 插件 | 独立日常助手，处理当前文件、当前报错、局部脚本和小范围测试 |
| 用户 | 批准 push、不可逆操作、大范围重构、核心流程变更，以及敏感或保护范围的最终边界 |

控制点不放在低权限沙箱上，而放在任务包、爆炸半径、Git 边界和 Codex 的事后 diff 审查上。

## 任务分级

| 等级 | 主体 | 文件修改 | 典型任务 | 验收 |
| :--- | :--- | :--- | :--- | :--- |
| S0 | VS 插件 Claude | 允许少量局部 | 当前文件、当前报错、简单脚本、局部文档 | VS 内局部验证；必要时转 Codex |
| S1 | Codex 调 CC | 允许 | 文档修正、简单 bug、测试补齐、局部配置整理 | Codex 审 `git status`、`git diff` 和 `git diff --check` |
| M1 | Codex 调 CC | 允许 | 独立脚本、小 CLI、局部模块实现、小型重构 | CC 自测，Codex 审 diff 并运行指定验证 |
| M2 | Codex 调 CC | 默认不改 | 方案探索、竞争设计、反向 review | 输出判断、风险和建议；Codex 最终裁决 |
| L | Codex 主导 | 视授权 | 仓库级规则、权限链路、workflow、核心策略 | Codex 主导，CC 只辅助调查、局部实现或 review |

判断标准是爆炸半径，不是任务难度。GLM-5.1 可以处理边界明确的中等复杂任务；高爆炸半径任务仍由 Codex 主导。

## Wrapper

入口为 `scripts/ai/cc-worker.ps1`，只支持短调用：

```powershell
.\scripts\ai\cc-worker.ps1 `
  -Mode implement `
  -Task "S1: 只修改 docs/workflows/foo.md，修正文档中 X 与 Y 的不一致；完成后列出改动、验证和剩余风险。" `
  -AllowWrite "docs/**" `
  -ValidationCommand "git diff --check"
```

参数要点：

- `-Mode implement|plan|review`：实现、方案探索或反向 review。
- `-AllowWrite`：普通允许修改范围；默认 `docs/**`，非文档任务必须显式声明范围，且永远不开放保护目录。
- `-ProtectedWrite`：显式开放 `run/**`、`QuantProject/**` 等保护目录的具体子范围；保护目录只能用这个参数开放。
- `-AllowCommit`：只有 Codex 任务包明确允许时，CC 才可 commit；wrapper 场景仍禁止 push。
- `-MaxBudgetUsd`：限制单次 Claude CLI 调用预算。
- `-OutputFormat text|json`：默认 text；json 只用于需要结构化输出的调用。

开关接口只管理 worker 启用状态，不调用 Claude，不读取任务包，不修改业务文件：

```powershell
.\scripts\ai\cc-worker.ps1 -Status
.\scripts\ai\cc-worker.ps1 -Disable -Reason "claude unavailable"
.\scripts\ai\cc-worker.ps1 -Enable
```

- 临时覆盖：`CODEX_CC_WORKER_ENABLED=0|1`，只影响当前进程环境；`1` 可临时覆盖持久 disabled。
- 持久状态：`.state/cc-work/cc-worker-control.json`，字段固定为 `enabled`、`reason`、`updatedAt`；该文件是本地状态，不纳入提交。
- worker 关闭时，普通调用返回明确的 `skipped` 状态并直接退出，不调用 Claude、不创建 wrapper log、不做 baseline fingerprint，也不修改工作区。

## Codex 主动评估

Codex 在本仓处理非琐碎 S1/M1/M2 任务时，默认先主动评估是否调用本 wrapper。这里的“主动”只表示高权重提示词偏好：Codex 决定合适后显式执行一次短调用；不是 hook、daemon、后台轮询、任务队列或每条 shell 命令自动触发。

适合主动评估并可能委派的任务：

- S1：边界清楚的文档修正、局部配置整理、简单 bug fix、测试补齐。
- M1：独立脚本、小 CLI、局部模块实现、小型重构，且允许写入范围能明确枚举。
- M2：方案探索、竞争设计、反向 review，默认不改文件。

默认跳过自动委派的任务：

- L 级或仓库级核心策略、规则链、权限模型和长期 workflow 设计。
- 敏感凭据、认证、token、cookie、API key、proxy secret、账号池、路由维护。
- 强耦合根因排查、需要主线程完整上下文连续推理的任务。
- Git 高危、发布、PR、merge、rebase、tag、amend、reset、clean、远端或历史操作。
- 允许写入范围无法清楚表达、需要跨多个保护域协调，或 worker 当前被 `-Disable` / env 关闭。

如果 worker 不可用、被暂停或评估后不适合，Codex 继续独立执行，并在收口时说明跳过原因。

## Codex 调用规范

调用方的 `-Task` 不需要重复长安全约束；边界由 wrapper 内部提示、参数、preflight 和 postflight 承担。**preflight 只拒绝明显越界意图，不证明任务安全；postflight 才以真实 Git diff / fingerprint 为事实裁判。** Codex 委派 CC 时不要绕过本 wrapper 直接调用 `claude -p`，否则模型会按普通用户任务执行，不能指望它自动继承本工作流的保护范围。

Codex 调用 CC 前必须写清：

```text
任务目标：
任务等级：S1/M1/M2/L辅助
允许修改范围：
保护范围是否开放：
是否允许 commit：
禁止事项：
验证命令：
输出要求：
不清楚时停止并说明，不要扩大范围猜测。
```

Codex 调用后必须执行：

```powershell
git status --short
git diff --stat
git diff
git diff --check
```

必要时再运行任务指定测试、lint 或脚本验证。Codex 不在 CC 修改同一批文件时并行编辑，避免双 agent 冲突。

## 权限策略

Codex 调用的 CC wrapper：

- 使用 `claude -p` 短调用。
- `implement` 默认使用 `--permission-mode acceptEdits`，给 GLM 宽松本地执行空间。
- `plan` 和 `review` 默认使用 `--permission-mode plan`，原则上不改文件。
- 默认不传 `--model`，继承当前 Claude Code provider / GLM-5.1 配置。
- 默认 Bash / PowerShell 宽松，允许排查、测试、lint、build 和常规本地命令；wrapper 显式禁止 `git push`、`gh pr`、`gh release`、`Edit`/`Write` 等高风险或模式不匹配工具。
- 永远禁止 push、PR 创建和发布；用户正常打开 Claude Code 并显式命令时不受本 wrapper 限制。
- 不把 Claude Code 的 sandbox 当强制安全边界；Windows 环境可能无 sandbox，控制点是任务包、postflight 和 Codex diff 审查。
- 边界优先级是 denylist > allowlist > postflight：敏感文件和未开放保护目录优先拒绝，普通写范围只在剩余范围内生效。

Git 边界：

- 普通 Git 读命令允许，例如 `git status`、`git diff`、`git log`、`git show`、`git branch --show-current`。
- `-AllowCommit` 未开启时，CC 不得 commit。
- `-AllowCommit` 开启时，CC 只能提交本轮任务改动，仍不得 push；提交中的路径也必须通过 `AllowWrite` / `ProtectedWrite` / 敏感路径策略。
- `git reset --hard`、`git clean`、rebase、amend、强制 checkout/switch、删除 branch/stash/worktree 仍属于高危操作，默认由 Codex 主导。

敏感边界：

- 不读取或修改 `.env`、`.env.*`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`、token、cookie、API key 或 proxy secret。
- 不修改仓库外路径。
- `run/**` 和 `QuantProject/**` 默认不作为普通任务写入面；默认 `-AllowWrite` 只开放 `docs/**`，`-AllowWrite "."` 不包含保护目录，`-AllowWrite "run/**"` 也会被拒绝。Codex 只能通过 `-ProtectedWrite` 明确开放具体子路径。

## 验收标准

- Codex 能运行 `.\scripts\ai\cc-worker.ps1 -Task "..."`。
- S1 docs 任务能只留下文档 diff。
- M1 边界明确的脚本或测试任务能留下可审查 diff，并运行指定验证。
- M2 方案探索或反向 review 默认不改文件。
- `-AllowCommit` 未开启时不 commit；开启时只允许本轮提交，不 push。
- `-AllowCommit` 开启时，postflight 会检查提交范围，不能通过提交后清理工作树绕过路径策略。
- Codex 调用的 CC 不 push、不创建 PR。
- 明显要求写入未开放 `run/**` 或 `QuantProject/**` 的任务会被 preflight 拒绝，Claude 不会被调用。
- 未开放的 `run/**` 或 `QuantProject/**` diff 会被 postflight 标记。
- 敏感文件出现在 diff 中会被 postflight 标记。
- `AllowWrite` 会被 postflight 执行检查；除保护目录外，本次调用实际改变的路径必须落在允许写入范围内；保护目录实际改动必须落在 `ProtectedWrite` 范围内。
- `plan` 和 `review` 模式会比较调用前后的工作树指纹；即使调用前已有 dirty 文件，也应能发现本次调用对同一路径的内容改动。
- 如果某个路径调用前已经 dirty，本次调用又继续修改同一路径，postflight 会把它列为 dirty overlap violation；默认不自动回滚。
- `-Status`、`-Enable`、`-Disable` 只管理 `.state/cc-work/cc-worker-control.json`，不调用 Claude。
- `CODEX_CC_WORKER_ENABLED=0` 或持久 disabled 时，普通调用返回 `skipped`，且不触发 Claude。
- `CODEX_CC_WORKER_ENABLED=1` 可临时覆盖持久 disabled。
- `-OutputFormat json` 时 wrapper 自身状态输出不污染 stdout 中的 Claude payload。
- Codex 调用后能完成 `git status --short`、`git diff --stat`、`git diff`、`git diff --check` 审查。

## 回滚

如需撤回本能力，删除 `scripts/ai/cc-worker.ps1` 和本文件，撤回入口索引，更新 support inventory 为 retired，再运行 `git diff --check` 和 support health。不要使用 broad reset 或清理命令回滚。
