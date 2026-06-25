# Codex Skills 白名单

本目录是 `claudecode` 仓库级 Codex skill 白名单入口。

当前保留的仓库级 skill：

- `karpathy-project-bridge`
- `karpathy-guardrail`
- `frontend-design-project-bridge`
- `repo-verification-before-completion`
- `repo-maintenance`
- `repo-local-pr-review`
- `repo-module-admission`
- `cleanup-worktrees`
- `scrapling-web-scraping`

详细触发场景见 `docs/当前规则/40-Agent与Skill.md`。没有列入本 README 的 skill 不视为默认启用。

## Boundary

- 默认使用简体中文输出计划、进展、风险、验证、审查和总结；生成计划文档、治理文档或任务总结时正文必须为简体中文，英文计划视为不合格。
- 不保留 memory / learning promotion。
- 不恢复旧 command、hook、自动 PR shipping、task resume 或高权限 worktree skill；`cleanup-worktrees` 只作为用户显性调用的 PR 合并残留清理入口，默认只清理已合并、无领先提交、无 tracked 修改、无 `??` untracked 的受管或仓库内临时 review 残留；普通 ignored runtime/cache/log/data 只报告、不阻断 stale worktree 整体移除，凭据类 ignored 文件仍阻断；`audit` 类请求保持只读。
- 新增 skill 必须先得到用户明确要求，并走 `repo-module-admission`。
- 不保留 Superpowers bridge skill。
- 其他 skill 不得覆盖 `AGENTS.md`、`docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md`、`docs/当前规则/40-Agent与Skill.md` 或用户本轮限制。
- 未获当前任务授权时，不触碰 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 或 `qm-run-demo`。
