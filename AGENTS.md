# claudecode Agent 规则

本文件是 Codex 在 `claudecode` 仓库内的不可违背规则摘要；现行 Superpowers 只认官方已安装 plugin，仓库只保留薄的 S/M/L 治理边界，不再保留桥接执行层。

## 不可违背规则

- 默认使用简体中文输出总结、风险、验证结果和变更说明。
- 开始任务先运行 `git status --short`；发现非本轮修改时先报告并避免混入。
- 仓库根目录是治理、路由和工具骨架；业务修改必须先落到 `docs/workflows/work_area_registry.md` 登记的明确工作区。
- `S/M/L` 只用于治理分级：`S` 为纯文字或说明性变更，`M` 为局部行为变更，`L` 为 workflow、agent 规则、plugin、hook、proxy、权限链路、仓库结构、关键数据/策略或跨模块修改。
- 官方 Superpowers plugin 是唯一 Superpowers 来源；仓库不再提供 bridge、wrapper 或本地 skill 作为第二来源。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`。
- 涉及 `Codex Proxy`、`cockpit-cliproxy`、Clash、mihomo、`7898`、`7899`、Codex 出口或账号池隔离的任务，必须先读 `docs/workflows/codex-egress-maintenance.md`；`7898` 是 Codex 专用隔离出口，不得指回 `7899`、`手动选择`、`GLOBAL` 或主选组，默认优先使用当前订阅 AI 出口组。
- `7899` 保持 Clash Verge 日常代理和 `手动选择` 语义，不作为 Codex 隔离出口；除非用户当前轮明确点名，不得为了 Codex 出口修改 `7899`。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动；commit 前只允许 `git add` 本轮修改文件，禁止 `git add .`。
- 未获明确授权时，不主动 push、创建/更新 PR、merge、删除远端分支或丢弃未合并成果。
- 用户明确要求 push、PR、merge 或清理指定 branch/worktree 时，该指令本身即授权；agent 必须自行执行必要命令并验证结果，不得退回给用户手工输入。
- 用户明确要求新建 worktree 时，该指令本身即构成本地 task worktree 与必要任务分支的创建授权；agent 按 `docs/workflows/worktree-policy.md` 的 managed root 和命名规则直接创建并验证，不再要求业务层确认。清理、移除、覆盖、强制 checkout 仍需用户单独点名。
- `force push`、`reset --hard`、删除/丢弃未合并成果、覆盖远端历史，必须被用户明确点名；一旦动作和目标明确，不额外增加业务层确认。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。
- commit message 与 PR 标题/正文使用中文 conventional 格式与中文三段式；具体 type 字典、分支中文别名行与例外见 `docs/workflows/git-language-policy.md`。

## 入口

- Codex：先读本文件、`PROJECT.md`、`docs/index.md`，再按薄 S/M/L 边界执行。
- Claude Code：只读 `CLAUDE.md` 的入口说明；该文件不得覆盖本文件和仓库规则。
- 完成报告必须列出：修改文件、是否触碰 `run/**` 或 `QuantProject/**`、是否执行删除/清理/移动、是否 staging/commit/push、验证命令与结果、剩余风险。
