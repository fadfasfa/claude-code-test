# Git 与高危操作

本文件只维护 `claudecode` 仓库特有的保护资产、工作树路径、PR 修复、清理和发布边界。Codex 的普通分支、工作树、提交与普通推送授权语义以用户级 `AGENTS.md` 为唯一通用事实源；Claude Code 继续使用自己的用户级授权规则，本次 Codex 适配不扩大其权限。

## 本仓 Git 基线

- 开始任务、保护无关脏改、精确暂存和完成后复核分别由根 `AGENTS.md` 与 `docs/当前规则/30-验证与审查.md` 约束，本文件不复述。
- 普通推送授权不包含 PR、merge、tag、release、force push、rebase、历史重写、remote 变更、远端分支删除或任何清理动作。
- 已登记的本地私有工作区与分支不得因普通提交或推送授权进入 public remote。

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
- `docs/当前规则/10-工作区登记.md` 中登记的其他本地私有工作区、分支和长期工作树；包括象棋 `codex/feat/xiangqi-local` 与 detached `xiangqi-screen-review`，默认不发布、不合并、不清理。
- 任何业务工作区内未授权修改。
- `auth.json`、token、cookie、API key、`.env`、`local.yaml`、`proxies.json`。
- 用户级 `.claude`、`.codex` 和 KB 仓库，除非用户明确纳入范围。

## 需要单独授权的高危操作

- PR 创建或更新、merge、amend、tag、release、force push、remote 变更、远端分支删除和历史重写；普通 `git push` 按用户级 `AGENTS.md` 的一次授权语义执行。
- discard / 清理类操作：`git reset --hard`、`git clean -fdx`、`git restore`、覆盖式 checkout、批量删除、批量移动、不可逆清理。
- 跨工作区重构、批量格式化、迁移生成物或删除运行态目录。
- 修改凭据、登录态、proxy、账号池或全局配置。

## 执行规则

- 破坏性命令必须由用户当前轮明确点名动作与目标；授权已经明确时按范围执行并验证，不再增加业务层确认。
- 删除、覆盖、移动前必须确认目标路径。
- 需要备份时先确认成功；备份失败停止依赖它的风险操作，允许只读诊断及不依赖该备份的工作。
- 不通过修改 ACL、绕过沙箱或写入未授权路径来补救失败备份。
- 即使人工选择高权限 profile，也不得绕过本仓高危资产规则。

## Worktree 路径与 Codex 使用边界

- Codex 通过命令行新建的 worktree 使用 `C:\Users\apple\worktrees\codex`，目录名包含仓库名与任务主题；新分支沿用用户级 `AGENTS.md` 的 `codex/` 命名。
- Claude Code worktree root：`C:\Users\apple\_worktrees\cc`。
- cleanup-only legacy worktree root：`C:\Users\apple\worktrees\claudecode-*`。该 root 只用于清理历史平铺 worktree，不作为新建 worktree 目标。
- Codex App 已创建的 worktree 保持原路径并优先复用，不为了统一目录而移动或重建。已有分支若绑定工作树，Codex 直接进入该工作树；没有绑定时可安全签出，不创建替代分支。
- Codex 发现同一任务已有分支或 worktree 且身份吻合时优先复用；自动命名碰撞时选择安全替代名称，用户指定目标与现有内容冲突时才停止询问，不覆盖。
- Codex 新建的 worktree 默认基于已提交内容，不默默迁移未提交改动、运行数据、依赖目录或凭据；任务依赖这些内容时明确报告缺口。
- Codex 完成任务后不自动合并、删除分支或清理 worktree。短期分支不设强制寿命；登记为本地私有或长期保留的分支和 worktree 不进入普通收尾清理。

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
- PR 标题与该 PR 主 commit 首行一致，即中文 conventional 格式。
- PR 正文使用三段中文小标题，顺序固定：`## 总结`、`## 验证`、`## 剩余风险`。
- 三段内容均使用陈述短句；无内容时写“无”，不留空标题。
- 不向仓库添加 `.github/pull_request_template.md`，避免影响非 agent 创建的 PR。
- GitHub Web UI 自动生成的 `Merge pull request #N from ...` commit 不改写。
- `git revert` / `git cherry-pick` 默认生成的 `Revert "..."` / `(cherry picked from ...)` 前缀保留英文；如手工补充说明，说明部分用中文。
- 第三方自动化产生的英文 commit 与 PR 不在本规则约束内；agent 不主动改写其历史。
- 用户当前轮明确要求英文 commit 或沿用上游英文模板时，该指令仅对该轮生效。
