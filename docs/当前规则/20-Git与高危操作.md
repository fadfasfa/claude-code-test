# Git 与高危操作

本文件合并 Git 策略、高危资产、高危操作、worktree 和发布边界。

## 默认 Git 规则

- 默认允许 `git status`、`git diff`、`git log` 等只读命令。
- 每次任务开始先运行 `git status --short`。
- Git 写操作必须有当前轮授权或已批准计划；授权已经明确时，agent 直接执行并验证，不重复要求业务层确认。
- commit 前只能暂存本轮修改文件，禁止 `git add .`。
- 未获用户明确授权时，不主动执行 `push`、PR、merge、discard、`clean`、`reset`、`rebase`、`stash`、`switch`、`branch` 或 worktree 写入。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## 明确授权

只有用户当前轮明确要求对应动作，或当前轮批准的任务计划已经包含该动作，才执行 commit、push、branch、checkout、switch、merge、rebase、reset、clean、restore、stash、tag、cherry-pick、revert、worktree、remote 或 PR 操作。

commit 授权不隐含 push，push 不隐含 PR 或 merge，discard 授权也不隐含其他清理动作。用户明确授权 push、PR、merge 或 discard 后，agent 必须按确认范围自行完整执行并验证结果，不得退回为“请你在本机终端执行命令”。

## PR 修复后的推送规则

- 当前轮用户明确要求修复已有开放 PR 的审查意见、requested changes、CI/check failure 或等价 PR 修复闭环时，任务默认包含必要 commit 和普通 `git push` 到当前 PR 分支；不需要用户另写 `push` / `推送`。
- 修复前必须确认当前分支对应唯一 open PR，且当前分支等于该 PR 的真实 `headRefName`。不匹配时停止，不得新建替代修复分支；允许把已存在的真实 PR head 分支绑定到新的 linked worktree。
- PR 只读审查使用 GitHub API、`FETCH_HEAD` 或不落持久分支的临时 ref；禁止用 `git fetch origin pull/<编号>/head:pr-<编号>` 或等价命令留下 `pr-<编号>` 本地分支。
- 触发条件必须是已有开放 PR 的修复闭环。普通“修复”“完成”“整理”“收尾”“验证”“本地 review”或“创建 PR”不构成 push 授权。
- 用户明确要求“只改不提交”“不推送”“只验证”或“只审查”时，以用户限制为准，不执行 commit 或 push。
- 推送前必须完成软检查：当前分支能对应唯一 open PR，且 PR head 分支等于当前分支；已运行最小有效验证和本地自审；准备推送前 staged 为空、无 tracked 未提交改动；既有 unrelated untracked 只报告，不暂存。
- 遇到以下任一情况必须停止并报告，不推送：不在 PR 分支、找不到唯一 open PR、PR head 不匹配；当前分支 behind 或 diverged，需要 rebase、merge 或 force 才能推送；验证失败、触碰未授权保护范围，或 commit 会混入无关改动。
- PR 修复后的推送授权只允许普通 `git push` 当前 PR 分支，不包含 PR merge、tag、release、force push、amend、rebase、历史重写、remote 变更或删除远端分支。

## 高危资产

- `run/**` 中的原始数据、不可重建资产和当前脏树。
- `sm2-randomizer/**` 业务工作区内未授权修改。
- `sms-monitor/**` 业务工作区内未授权修改。
- `heybox/**` 业务工作区内未授权修改。
- `subtitle_extractor/**` 业务工作区内未授权修改。
- `QuantProject/**` 本地私有工作区，默认不发布到 public remote。
- `qm-run-demo` 示例发布仓库，当前只保留 gitlink，不在普通任务中维护 `.gitmodules` 或子仓内容。
- 任何业务工作区内未授权修改。
- `auth.json`、token、cookie、API key、`.env`、`local.yaml`、`proxies.json`。
- 用户级 `.claude`、`.codex` 和 KB 仓库，除非用户明确纳入范围。

## 高危操作

- `git push`、PR、merge、amend、tag、release。
- discard / 清理类操作：`git reset --hard`、`git clean -fdx`、`git restore`、覆盖式 checkout、批量删除、批量移动、不可逆清理。
- 跨工作区重构、批量格式化、迁移生成物或删除运行态目录。
- 修改凭据、登录态、proxy、账号池或全局配置。

## 执行规则

- 破坏性命令必须有当前轮明确授权或已批准计划；授权已经明确时，agent 按范围执行并验证，不重复要求业务层确认。
- 删除、覆盖、移动前必须确认目标路径。
- 需要备份时，先确认备份成功；备份失败即停止。
- 不通过修改 ACL、绕过沙箱或写入未授权路径来补救失败备份。
- 即使人工选择高权限 profile，也不得绕过本仓高危资产规则。

## Worktree 边界

- 不自动创建长期 worktree。
- 用户明确要求新建 worktree 时，默认使用 `codex/` 分支前缀和 managed root。
- Codex worktree root：`C:\Users\apple\worktrees\codex`。
- Claude Code worktree root：`C:\Users\apple\_worktrees\cc`。
- cleanup-only legacy worktree root：`C:\Users\apple\worktrees\claudecode-*`。该 root 只用于清理历史平铺 worktree，不作为新建 worktree 目标。
- 默认创建新分支，不在主工作树执行 `checkout` / `switch`。
- 目标路径、目标分支或目标 worktree 已存在时停止，不覆盖。
- 清理、移除、强制覆盖或修改受管 metadata 必须有用户当前轮单独点名授权。

## cleanup-worktrees 边界

完整执行合同（审计与合并判定、squash 交叉验证、fetch 标记、原因码、输出格式）以两份 `cleanup-worktrees/SKILL.md` 为准；两份正文必须保持一致，并服从本节硬边界：

- 审计模式（`audit`、`--dry-run`、`--audit`、`只审计`）只输出结果，不得 fetch、不得删除。
- 只对硬干净、位于允许 root 且非 protected 的 worktree 执行普通 `git worktree remove`；祖先合并分支用 `branch -d`；已验证 squash 分支用带 expected-old-OID 的 `git update-ref -d`。
- stale `origin/*` 必须先确认 GitHub 上真实远端分支不存在，才可逐项删除。
- 不自动丢弃 dirty 内容，丢弃需用户当前轮精确授权具体路径；凭据类 ignored 文件阻断且不得读取内容。
- 不得删除真实远端分支，不得升级到 `git worktree remove --force`、`branch -D`、`git fetch --prune`、`git remote prune origin`、未限定 remote/ref 的 prune、`git clean` 或 `reset --hard`。

## Commit 和 PR 语言

- commit message 格式：`<中文type>(<可选 scope>): <中文描述>`。
- type 字典固定为：`修复`、`新增`、`文档`、`重构`、`杂项`、`试探`、`测试`。
- scope 可选；优先用中文模块名，原始目录名或包名可读性更高时保留英文。
- 首行不超过 72 字符，结尾不加句号。
- 正文与首行之间空一行；正文使用陈述句，必要时分行列出关键事实，不使用 emoji。
- 新建分支后的首个 commit，正文必须包含独立一行：`分支: <英文分支名>（<中文别名>）`。
- 英文分支名沿用 `cc/<type>/<slug>` 或 `codex/<type>/<slug>`，不变；中文别名格式为 `<中文type>-<中文 scope 或主题>`。
- 同一分支的后续 commit 不必重复中文别名行。
- PR 标题与该 PR 主 commit 首行一致，即中文 conventional 格式。
- PR 正文使用三段中文小标题，顺序固定：`## 总结`、`## 验证`、`## 剩余风险`。
- 三段内容均使用陈述短句；无内容时写“无”，不留空标题。
- 不向仓库添加 `.github/pull_request_template.md`，避免影响非 agent 创建的 PR。
- GitHub Web UI 自动生成的 `Merge pull request #N from ...` commit 不改写。
- `git revert` / `git cherry-pick` 默认生成的 `Revert "..."` / `(cherry picked from ...)` 前缀保留英文；如手工补充说明，说明部分用中文。
- 第三方自动化产生的英文 commit 与 PR 不在本规则约束内；agent 不主动改写其历史。
- 用户当前轮明确要求英文 commit 或沿用上游英文模板时，该指令仅对该轮生效。
