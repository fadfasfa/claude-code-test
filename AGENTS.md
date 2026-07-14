# claudecode Agent 规则

本文件是 Codex 在 `claudecode` 仓库内的常驻硬边界摘要。任务文档、工作区清单、脚本说明和细节流程由 `docs/` 索引发现，不在本文件重复维护。

## 不可违背规则

- 默认使用简体中文输出计划、进展、问题、风险、验证结果、审查结论和最终总结；生成计划文档、治理文档或任务总结时正文必须为简体中文，除非用户明确要求其他语言；英文计划视为不合格，交付前必须改为中文。
- 开始任务先运行 `git status --short`；发现非本轮修改时先报告并避免混入。
- 仓库根目录是治理、路由和工具骨架；业务修改必须落到已登记的明确工作区。
- `S/M/L` 只用于治理分级，不恢复旧 bridge 流程入口或权威流程。
- `brainstorming` 与 `karpathy-guidelines` 由 Codex、Claude Code 各自的原生目录提供；仓库不为这两个基线 Skill 建立 bridge、wrapper 或第二事实源。
- 不读取或修改凭据、token、auth、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。
- 不把 Codex 出口、代理、账号池或路由维护混入普通仓库实现任务；本文件不保存 live proxy 细节、端口策略或维护步骤。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动；commit 前只允许 `git add` 本轮修改文件，禁止 `git add .`。
- 未获明确授权时，不主动 push、创建/更新 PR、merge、删除远端分支、丢弃未合并成果或执行难恢复 Git 操作。
- 用户明确授权 push、PR、merge、新建 worktree 或清理指定 branch/worktree 时，agent 必须自行执行必要命令并验证结果，不退回给用户手工输入。
- 当用户当前轮明确要求修复已有开放 PR 的审查意见、requested changes、CI/check failure 或等价 PR 修复闭环时，任务默认包含验证、自审、必要 commit 和普通 `git push` 到当前 PR 分支；不需要额外出现 `push` / `推送` 字样。
- 该默认授权只适用于当前分支能对应唯一 open PR 且 PR head 分支等于当前分支；泛化“修复”“完成”“整理”“验证”“本地 review”“创建 PR”不构成 push 授权，用户明确要求“只改不提交”“不推送”“只验证”“只审查”时以用户限制为准。
- PR 修复后的推送授权不包含 PR merge、tag、release、force push、amend、rebase、历史重写、remote 变更或删除远端分支。
- 长任务应按合适阶段拆分；每阶段完成后先自检，再使用子智能体审查，通过后进入下一阶段。
- `force push`、`reset --hard`、删除/丢弃未合并成果、覆盖远端历史，必须被用户明确点名；一旦动作和目标明确，不额外增加业务层确认。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。
- commit message 与 PR 标题/正文使用本仓约定的中文 conventional 格式；具体格式、例外和流程由 workflow 文档维护。

## Codex worker 退役状态

- Codex 当前不主动调用 Claude Code worker；旧短调用 wrapper 和说明已归档到 `docs/历史归档/cc-worker/`。
- 归档内容只作为历史和恢复材料，不是 active workflow、hook、daemon、任务队列或命令拦截入口。
- 如需恢复，先按 archive README 反向移回脚本和 workflow，再同步入口索引、support inventory 和验证结果。

## 入口和收口

- Codex：先读本文件、`PROJECT.md` 和 `docs/index.md`，再按任务相关文档执行。
- Claude Code：只读 `CLAUDE.md` 的入口说明；该文件不得覆盖本文件和仓库规则。
- 完成报告必须列出：修改文件、是否触碰 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 或 `qm-run-demo`、是否执行删除/清理/移动、是否 staging/commit/push、验证命令与结果、子智能体审查结果和剩余风险。
