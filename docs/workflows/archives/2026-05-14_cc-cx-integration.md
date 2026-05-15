> Status: 2026-05-14
> Phase 1 (Karpathy three-tier), Phase 3 (cx-exec + protocol),
> Phase 7 (explicit-only worktree policy) — completed and merged.
> Phase 6 verified via pure-CX dry-run in main repo on 2026-05-14.

# CC + CX 双代理集成 MVP 计划

## 1. Local Facts

### 1.1 原始现状

1. `C:\Users\apple\.claude\CLAUDE.md` 不存在。
2. `C:\Users\apple\claudecode\CLAUDE.md` 存在；`C:\Users\apple\claudecode\.claude\CLAUDE.md` 不存在。
3. `C:\Users\apple\claudecode\.claude\settings.json` 存在，`settings.local.json` 不存在；可见权限相关项为 granular approval + workspace-write + `windows.sandbox="unelevated"`，未见 `hooks` 段。
4. `C:\Users\apple\claudecode\.claude\skills\` 不存在；`.claude` 下只有 `README.md`。
5. `C:\Users\apple\claudecode\.agents\skills\superpowers-project-bridge` 与 `karpathy-project-bridge` 都存在。
6. `C:\Users\apple\claudecode\AGENTS.md` 的 Methodology Router 已覆盖 brainstorming / using-git-worktrees / writing-plans / TDD / debugging / executing-plans / requesting-code-review 七项。
7. `scripts\workflow\` 现有脚本包含 `cleanup-worktree.ps1`、`finalize-pr.ps1`、`local-review.ps1`、`task-metadata.ps1`、`verify.ps1`、`worktree-start.ps1`、`worktree-status.ps1`；`acceptance_gate` 计算在 `local-review.ps1`，初始化在 `worktree-start.ps1`，执行约束在 `finalize-pr.ps1`。
8. 没有独立 `.task-worktree.schema.json`；当前 `.task-worktree.json` 由 `task-metadata.ps1` 只做必需字段存在性校验，不拒绝额外字段。
9. `C:\Users\apple\.codex-exec` 存在；`config.toml` 存在，`auth.json` 不存在。
10. `C:\Users\apple\.codex-exec\config.toml` 中存在 `codex-proxy` provider，`base_url = "http://127.0.0.1:8080/v1"`。
11. VS Code 的 `chatgpt.cliExecutable` 指向 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`；该 wrapper 为二进制，源码不可读。
12. `codex` 解析到 `C:\Users\apple\AppData\Roaming\npm\codex.ps1`，`codex.exe` 解析到 `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`；`codex --version` 为 `codex-cli 0.121.0`。
13. `pwsh -v` 为 `7.6.1`，`$PSVersionTable.PSVersion` 也是 `7.6.1`。

### 1.2 CC 独立接入 codex proxy 的最小要求

a. 监听与命名：Codex Proxy API 端口是 `8080`，本机上游代理是 `7899`。现有文档没有给出 `client-id`、`X-Codex-Source` 或按来源拆分 provider 的方案；当前接入只靠隔离 `CODEX_HOME` 与 wrapper，不靠请求头分流。

b. VS 插件现状：VS 插件使用 `C:\Users\apple\.codex-exec\config.toml`，关键字段是 `model_provider = "codex-proxy"`、`base_url = "http://127.0.0.1:8080/v1"`、`env_key = "CODEX_PROXY_API_KEY"`、`wire_api = "responses"`；认证读取路径由 `CODEX_PROXY_API_KEY` 环境变量决定，`auth.json` 不参与这条路径。

c. Codex App 路径：文档没有给出 Codex App 自己的固定 `CODEX_HOME`；只明确它不是 VS 插件路由的维护面。计划里将 CC 的独立目录命名为 `C:\Users\apple\.codex-exec-cc`，避免和 VS 的 `.codex-exec` 冲突。

d. 第三类客户端预留点：文档只有“隔离 Codex home + wrapper”这一通用模式，没有第三类客户端的专门命名或单独接入流程；因此 plan 按通用方案自行命名 CC 的独立 `CODEX_HOME`。

e. 禁止项：不写全局 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `CODEX_HOME`；不读取或修改 auth/token/cookie；不让 CC 复用 VS 插件目录；不把 UI banner 或账号显示当作入账证据。

### 1.3 认证读取路径

- `codex exec` 支持两种输入方式：命令行 prompt 参数，或不带 prompt 时从 stdin 读取；未发现 `--prompt-file`。
- 在最小可用的 `codex-proxy` 配置里，`codex exec` 直接检查 `CODEX_PROXY_API_KEY` 是否存在。
- 临时目录实验中，`CODEX_PROXY_API_KEY` 存在时，`codex exec` 可成功完成无害 prompt；缺失时直接报 `Missing environment variable: CODEX_PROXY_API_KEY.`。
- 结论：CC 独立 `CODEX_HOME` 模式下，认证读取路径是 `CODEX_PROXY_API_KEY` 环境变量，不是 `auth.json`。

## 2. MVP Scope

Phase 0 已完成：只读盘点、文档收敛、计划写入。

Phase 1，Karpathy 三层接入：
- 新建或更新 `C:\Users\apple\.claude\CLAUDE.md`
- 更新 `C:\Users\apple\claudecode\CLAUDE.md`
- 新建 `C:\Users\apple\claudecode\.claude\skills\karpathy-guardrail\SKILL.md`

Phase 3，精简版 CC + CX orchestration：
- 新建 `scripts\workflow\cx-exec.ps1`
- 新建 `tests\workflow\cx-exec.Tests.ps1`
- 新建 `docs\workflows\10-cc-cx-orchestration.md`
- 更新 `scripts\workflow\worktree-start.ps1`

Phase 6，纯 CX 独立路径 dry-run：
- 在新建 worktree 中以任意 acceptance_gate（推荐 `automated`）跑完整链路
- 全程不调用 `cx-exec.ps1`，不创建 `CODEX_PROMPT.md`，不期待 `CLAUDE_REVIEW.md`
- 验证 `worktree-start -> verify -> local-review -> finalize-pr` dry-run 不因 Phase 1 / 3 的引入产生回归

## 3. Out-of-MVP

- Phase 2 的四个 cc-skill：`cc-brainstorming`、`cc-writing-plans`、`cc-debugging`、`cc-review`
- Phase 4：acceptance gate 引入 `cc-supervised` 状态与 fallback 降级逻辑
- Phase 5：PreToolUse hooks，包括 secret deny 与业务代码 deny

延后原因：第一批只验证“CC 能独立接入 codex proxy，且不和 VS 插件共用目录”这条主命题。

## 4. Phase-by-Phase plan

### Phase 1: Karpathy 三层接入

Files:
- `C:\Users\apple\.claude\CLAUDE.md`
- `C:\Users\apple\claudecode\CLAUDE.md`
- `C:\Users\apple\claudecode\.claude\skills\karpathy-guardrail\SKILL.md`

Plan:
- 全局 `.claude/CLAUDE.md` 只保留 4 条 Karpathy 原则，保持跨仓 always-on，但不写入 `codex-proxy`、`run/**` 或仓库路径。
- 仓库根 `CLAUDE.md` 明确 CC 的 planner / supervisor / reviewer 角色，并把调用 CX 的入口锁到 `cx-exec.ps1`。
- 在“调用 CX 必须通过 `cx-exec.ps1`”之后补充：`cx-exec.ps1` 通过 CC 自己的独立 `CODEX_HOME` 接入 codex proxy；不复用 VS Codex 插件的 `.codex-exec`，也不读取或修改 VS 插件、Codex App 的 auth / session / config 文件。

### Phase 3: Minimal orchestration

Files:
- `scripts\workflow\cx-exec.ps1`
- `tests\workflow\cx-exec.Tests.ps1`
- `docs\workflows\10-cc-cx-orchestration.md`
- `scripts\workflow\worktree-start.ps1`

Plan:
- `cx-exec.ps1` 使用 CC 独立的 `CODEX_HOME`，默认命名为 `C:\Users\apple\.codex-exec-cc`。
- `cx-exec.ps1` 首次运行时检测到 `config.toml` 缺失，则按模板创建；存在则不覆盖。
- 模板直接硬编码进 `cx-exec.ps1`，不新增单独模板文件；模板字段包含 `model`、`model_provider`、`name`、`base_url`、`env_key`、`wire_api`，并且只指向 `http://127.0.0.1:8080/v1`。
- `cx-exec.ps1` 通过 `codex exec` 非交互模式运行，输入来自 `CODEX_PROMPT.md`，建议以 stdin 方式传入；stdout / stderr 分别捕获后合并写入 `CODEX_RESULT.md`，并保留原始错误信息。
- `cx-exec.ps1` 前置 assert：当前 cwd 必须在 git worktree 内；该 worktree 必须是单一 active worktree；缺失 `CODEX_PROMPT.md` 必须 fail-fast；禁止 stage / commit / push / PR；不得引用或读取 `C:\Users\apple\.codex-exec\`。
- Pester 覆盖四个最小行为：非 active worktree 立即失败、缺 `CODEX_PROMPT.md` 立即失败、mock codex 验证 `CODEX_HOME` 选到 CC 独立目录、静态检查不允许 `git commit` / `git push` / `gh pr`。
- `cx-exec.ps1` 在独立 `CODEX_HOME` 下若 `CODEX_PROXY_API_KEY` 缺失，必须 fail-fast，错误提示引导用户设置该环境变量，不提 `codex login`。
- `auth.json` 不由 `cx-exec.ps1` 创建或写入。

### Phase 6: Pure CX dry-run

Files:
- no new files expected; reuse existing workflow scripts and Phase 1 / 3 artifacts

Plan:
- 在一个新 worktree 中以现有 workflow 直接跑 `worktree-start -> verify -> local-review -> finalize-pr` dry-run。
- 不调用 `cx-exec.ps1`，不创建 `CODEX_PROMPT.md`，不期待 `CLAUDE_REVIEW.md`。
- 验证 Phase 1 / 3 的加入没有改变原有纯 CX 流程的结果。

## 5. Design Decisions

A. `cx-exec.ps1` 入口
- 选择：直接调用 `codex.exe`，并在脚本内显式设置 `$env:CODEX_HOME = "C:\Users\apple\.codex-exec-cc"`。
- 依据：维护文档明确要求隔离配置和 wrapper 级进程环境变量；VS 插件继续使用 `C:\Users\apple\.codex-exec`，CC 必须独立接入，不能共用目录。
- `cx-exec.ps1` 不得读写 VS 插件 `.codex-exec` 的任何文件。

B. `cx-exec.ps1` 前置 assert
- 选择：当前 cwd 必须在 git worktree 内；该 worktree 必须是单一 active worktree；缺失 `CODEX_PROMPT.md` 必须 fail-fast；禁止 stage / commit / push / PR。
- 依据：这些是流程边界，不应依赖人工记忆。

C. 文件协议字段
- 选择：`CODEX_PROMPT.md`、`CODEX_RESULT.md`、`CLAUDE_REVIEW.md` 只保留用户定义的最小字段。
- 依据：协议越小，CC/CX 之间越稳定，越适合后续扩展。

D. CC 断流定义
- 选择：本 plan 不引入 CC 状态自动检测，也不读写、维护任何 CC 状态字段；用户手动决定何时走纯 CX workflow。
- 依据：新约束要求流程层对 CC 状态完全无感知。

E. Karpathy 三层接入边界
- 选择：全局 `.claude/CLAUDE.md` 只放 4 条 Karpathy 原则；仓库 `CLAUDE.md` 放 CC 角色与 `cx-exec.ps1` 入口；`karpathy-guardrail` 只做按需触发。
- 依据：全局保稳定原则，仓库层保角色与入口，skill 只在高风险上下文里补防线。

G. `local-review.ps1` 是否改动
- 选择：不改。
- 依据：现有 `local-review.ps1` 已只看 `target_paths` / `allowed_paths`，不会因为引入 CC 的协议文件而误判；把 CC/CX 协议文件放进 `worktree-start.ps1` 的 handoff 模板即可，不需要在 `local-review.ps1` 里额外排除。
- 结果：Phase 3 文件清单不再包含 `local-review.ps1`，只保留 `cx-exec.ps1`、测试、文档和 `worktree-start.ps1`。

## 6. Risks

- Claude Code 的 PreToolUse hook 是否支持按路径 glob deny 仍未验证；若不支持，Phase 5 需要替代方案。
- `cx-exec.ps1` 在 Windows + Codex sandbox 下是否需要显式 `--sandbox workspace-write` 仍需验证。
- VS Codex wrapper `codex-exec-wrapper.exe` 不能读源码；其 `CODEX_HOME` 处理方式只能靠文档和黑盒测试推断，若实际行为与文档不一致需单独追踪。
- CC 独立 `CODEX_HOME` 首次启用需要 `CODEX_PROXY_API_KEY`；`cx-exec.ps1` 在该环境变量缺失时必须 fail-fast，并提示用户手动设置，禁止脚本代登录。
- 多 worktree 环境下，cwd 误用其他 worktree 的风险真实存在，所以 `cx-exec.ps1` 的前置 assert 不能省。
- `C:\Users\apple\.claude\CLAUDE.md` 若已有内容，merge 时必须保留其他全局规则，不得只覆盖本任务内容。
- 若未来 Codex App 默认使用 `~/.codex` 或其他固定路径，CC 的独立 `CODEX_HOME` 仍属隔离，但三类客户端的 `CODEX_HOME` 路径需要同步记录到 `codex-maintenance` 文档，以便排查。

## 7. Acceptance Criteria per phase

### Phase 1

- `C:\Users\apple\.claude\CLAUDE.md` 包含 4 条 Karpathy 原则。
- `Select-String -Path C:\Users\apple\.claude\CLAUDE.md -Pattern 'codex-proxy|run/\*\*|claudecode|worktrees'` 无输出。
- `C:\Users\apple\claudecode\CLAUDE.md` 包含 CC 角色定义与“调用 CX 必须通过 `cx-exec.ps1`”，并补上“CC 使用自己的独立 `CODEX_HOME`，不复用 VS `.codex-exec`”。
- `C:\Users\apple\claudecode\.claude\skills\karpathy-guardrail\SKILL.md` 存在，且触发条件覆盖 non-trivial code change / architecture plan / debugging plan / review of Codex-generated changes。

### Phase 3

- `scripts\workflow\cx-exec.ps1` 存在并通过 Pester：
  - 非 active worktree 中运行立即 fail
  - 缺 `CODEX_PROMPT.md` 立即 fail
  - mock codex 验证 `CODEX_HOME` 被正确设置到 CC 独立目录
  - 静态检查中无 `git commit` / `git push` / `gh pr`
  - 静态检查中无 `\.codex-exec\` 字面量引用
  - 独立 `CODEX_HOME` 下 `CODEX_PROXY_API_KEY` 缺失时 fail-fast，错误信息引导用户设置环境变量
- `docs\workflows\10-cc-cx-orchestration.md` 包含三份协议文件的最小字段。
- `worktree-start.ps1` 生成的 `TASK_HANDOFF.md` 模板包含 `CODEX_PROMPT.md` / `CODEX_RESULT.md` / `CLAUDE_REVIEW.md` 的路径说明。
- `worktree-start` / `verify` / `finalize-pr` 行为不因 Phase 1 / 3 回归。

### Phase 6

- 新建 worktree 中以纯 CX 链路跑完 `worktree-start -> verify -> local-review -> finalize-pr` dry-run。
- 过程中不产生 “CC required” / “CLAUDE_REVIEW.md missing” 类错误，因为这条路径根本不感知 CC。

## 8. Rollback plan

- Phase 1 的三个文件都可独立删除或还原；删除 `karpathy-guardrail` 不影响现有 workflow。
- `cx-exec.ps1` 为新增脚本，回滚只需删除文件。
- 如果独立 `CODEX_HOME` 的最小配置或认证行为引入回归，优先回退 `cx-exec.ps1` 对该目录的创建逻辑，保留文档协议。
- 如果 Phase 3 的模板改动引入行为回归，优先回退 `worktree-start.ps1` 的 handoff 模板拼装，再保留 `cx-exec.ps1` 与协议文档。

## 9. Phase 7 Findings

### 9.1 Read-only inventory

a. `C:\Users\apple\claudecode\AGENTS.md` Methodology Router 中 using-git-worktrees 触发条件原文：

```text
- `using-git-worktrees`：默认在主仓工作。开 worktree 是显性动作，仅在以下两种触发下进行：用户消息中包含明示词“开树”、“在工作树里做”、“detached worktree”、“使用 worktree”之一；或上游 plan 文件（`IMPLEMENTATION_PLAN.md` / `TASK_BRIEF.md` / `CODEX_PROMPT.md`）明确标注 `requires_worktree: true` 或等价中文“需要 worktree”。
- 不构成开树触发：任务涉及多个文件、任务跨多个阶段、任务被 `writing-plans` / `executing-plans` 路由命中、任务被判定为 non-trivial。
- `scripts/workflow/worktree-start.ps1` 仍然可被显性调用；其行为不变。
```

b. `C:\Users\apple\AGENTS.md` 中相关条目原文：

```text
- Default to working in the main repository. Creating a worktree is an explicit-only action: do it only when the user message contains “开树”, “在工作树里做”, “detached worktree”, or “使用 worktree”, or when an upstream plan file (`IMPLEMENTATION_PLAN.md` / `TASK_BRIEF.md` / `CODEX_PROMPT.md`) explicitly marks `requires_worktree: true` or the equivalent Chinese “需要 worktree”.
- Multi-file work, multi-phase work, `writing-plans` / `executing-plans` routing, or a non-trivial task classification do not trigger tree creation.
- `worktree-start.ps1` remains available for explicit invocation and its behavior is unchanged.
```

c. `C:\Users\apple\claudecode\.agents\skills\superpowers-project-bridge\SKILL.md` 中 using-git-worktrees 触发条件及其与 writing-plans / executing-plans 的耦合方式：

```text
- `using-git-worktrees` 的策略：默认在主仓工作。开 worktree 是显性动作，仅在以下两种触发下进行：用户消息中包含明示词“开树”、“在工作树里做”、“detached worktree”、“使用 worktree”之一；或上游 plan 文件（`IMPLEMENTATION_PLAN.md` / `TASK_BRIEF.md` / `CODEX_PROMPT.md`）明确标注 `requires_worktree: true` 或等价中文“需要 worktree”。
- 不构成开树触发：任务涉及多个文件、任务跨多个阶段、任务被 `writing-plans` / `executing-plans` 路由命中、任务被判定为 non-trivial。
- `scripts/workflow/worktree-start.ps1` 仍然可被显性调用；其行为不变。
```

结论：该 skill 已无 writing-plans / executing-plans 自动联动开树的肯定措辞，但仍需同步本轮新增的“高风险路径”负向触发边界。

d. `C:\Users\apple\claudecode\CLAUDE.md` 中已有 worktree 使用时机描述：

```text
- CC 决定要让 CX 进入 worktree 时，必须在 plan 文件中显式写明 `requires_worktree: true`，并等用户 ack；不得在未声明的情况下让 `cx-exec.ps1` 在新 worktree 中执行。
```

### 9.2 Revised strategy

默认在主仓工作。开 worktree 是显性动作，仅在以下两种触发下进行：

1. 用户消息中包含明示词："开树"、"在工作树里做"、"detached worktree"、"使用 worktree" 之一；
2. 上游 plan 文件（IMPLEMENTATION_PLAN.md / TASK_BRIEF.md / CODEX_PROMPT.md）明确标注 "requires_worktree: true" 或等价中文 "需要 worktree"。

以下情形不构成开树触发：

- 任务涉及多个文件
- 任务跨多个阶段
- 任务被 writing-plans / executing-plans 路由命中
- 任务被判定为 non-trivial / 高风险路径

worktree-start.ps1 仍可被显性调用；其行为不变。
