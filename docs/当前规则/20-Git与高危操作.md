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

- 当前轮用户明确包含 `push`、`推送`、`修复并推送` 或等价发布指令时，agent 在修复 PR 问题后必须完成自检、自审、必要 commit，并自动推送当前 PR 分支。
- 泛化请求如“修复”“完成”“整理”“收尾”“验证”不构成 push 授权。
- push 授权不隐含 PR merge、tag、release、force push、amend、rebase 或历史重写。

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
- 默认创建新分支，不在主工作树执行 `checkout` / `switch`。
- 目标路径、目标分支或目标 worktree 已存在时停止，不覆盖。
- 清理、移除、强制覆盖或修改受管 metadata 必须有用户当前轮单独点名授权。

## cleanup-worktrees 边界

`cleanup-worktrees` 对话入口默认只清理 PR 合并后的本地残留：已合并到 base、相对 base 无领先提交、无 tracked 修改、无 `??` untracked，且 ignored 输出为空或仅为白名单 `__pycache__/` 缓存、受管且非 protected 的 worktree / 本地分支，以及已并入 base 的 stale `origin/*` 本机缓存。

`audit`、`--dry-run`、`--audit`、`只审计` 只审计。未合并、仍领先、dirty、存在 `??` untracked、存在非白名单 ignored 文件或目录、非受管、protected 或 GitHub 上真实存在的远端分支只列清单并保持不变。`git ls-remote --heads origin` 失败时跳过 stale remote ref 清理。不得删除真实远端分支，不得升级到 force remove、`branch -D`、`git clean` 或 `reset --hard`。

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
