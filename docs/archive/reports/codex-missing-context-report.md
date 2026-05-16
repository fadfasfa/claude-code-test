# Codex Missing Context Report

## 1. Executive Summary

- 当前最大缺口不是目录布局，而是执行入口不一致：根目录 `cx-exec.ps1` 仍是 mock；`scripts/workflow/cx-exec.ps1` 会调用 `codex exec`，但走 PATH 上的 npm `codex.ps1`，不是 C# wrapper。
- VS Code C# wrapper 位于 `C:\Users\apple\codex-maintenance\`，启动的是 `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`，并设置 `CODEX_HOME=C:\Users\apple\.codex-exec`。
- PowerShell `cx-exec.ps1` 默认 `CODEX_HOME` 是相对目录 `.codex-exec-$USERNAME`，不等于 wrapper 使用的 `C:\Users\apple\.codex-exec`，因此默认不会读取同一份 proxy config。
- `C:\Users\apple\.codex-exec\config.toml` 里存在 `codex-proxy` provider，`base_url=http://127.0.0.1:8080/v1`，`env_key=CODEX_PROXY_API_KEY`，`wire_api=responses`。
- 当前 shell 中 `CODEX_PROXY_API_KEY` 为 set；`http://127.0.0.1:8080/health` 返回 `status=ok`、`authenticated=true`、`pool.total=9`、`pool.active=9`。
- 本轮没有执行真实 `codex exec`，因此没有复现 401；只确认了入口、配置和 health。若要定位 401，下一步应先统一 `CODEX_HOME` 和 codex 入口，再运行最小真实 exec。
- 根目录已有 `CODEX_RESULT.md`、`CLAUDE_REVIEW.md`、`TASK_HANDOFF.md`、`diagnosis.log`、`.task-worktree.json` 等临时/状态产物；建议后续迁入 `.workflow/`，本轮不移动。
- 本次报告生成前 `.workflow/`、`.workflow/tasks/`、`.workflow/state/`、`.workflow/reports/` 均不存在；本轮仅按授权创建 `.workflow/reports/codex-missing-context-report.md`。

## 2. Facts Confirmed

- 仓库根目录存在 `cx-exec.ps1`。来源：`Get-ChildItem -Force -Name`，cwd=`C:\Users\apple\claudecode`。
- `scripts/workflow/cx-exec.ps1` 存在。来源：`Get-ChildItem -Force -LiteralPath C:\Users\apple\claudecode\scripts\workflow`。
- 两个 `cx-exec.ps1` 内容不一致。来源：`Get-FileHash -Algorithm SHA256`：
  - `C:\Users\apple\claudecode\cx-exec.ps1` = `4BC1249D15D7ED28ED7831776C5CC12AFDF0131630DE7F92D48EB216B8899169`
  - `C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1` = `35A76157F507BA2A194DCA4AE76992E1FCAE55E37CBC0DDE45B90280C408A745`
- 根目录 `cx-exec.ps1` 是 mock/smoke 脚本：它注释写明 `Simulates Codex (CX) executing a task`，并直接 `Set-Content` 生成 `docs/workflows/99-pipeline-smoke-test.md` 和 `CODEX_RESULT.md`。来源：`C:\Users\apple\claudecode\cx-exec.ps1`。
- `scripts/workflow/cx-exec.ps1` 会尝试真实调用 `codex exec "$TaskDescription"`，并在成功或失败后写 `CODEX_RESULT.md`。来源：`C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1`。
- `scripts/workflow/cx-exec.ps1` 通过 `Get-Command codex` 查找 codex；当前 shell 解析到 `C:\Users\apple\AppData\Roaming\npm\codex.ps1`。来源：`Get-Command codex`。
- 当前 PATH 上的 `codex` 版本是 `codex-cli 0.121.0`。来源：`codex --version`。
- C# wrapper 源码与 exe 存在于 `C:\Users\apple\codex-maintenance\`。来源：`Get-ChildItem -Force -LiteralPath C:\Users\apple\codex-maintenance | Where-Object Name -like codex-exec-wrapper*`。
- 仓库内未发现 `scripts/workflow/codex-exec-wrapper.cs` 或 `scripts/workflow/codex-exec-wrapper.exe`。来源：`Test-Path`/`Get-Content` 检查。
- C# wrapper 源码固定 `CODEX_HOME=C:\Users\apple\.codex-exec`。来源：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`。
- C# wrapper 源码固定真实 Codex exe 为 `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`。来源：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`。
- C# wrapper exe 可启动并返回 `codex-cli 0.130.0-alpha.5`。来源：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe --version`。
- `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe` 存在。来源：`Get-Item`。
- C# wrapper 源码只显式覆盖 `CODEX_HOME`，没有删除 `CODEX_PROXY_API_KEY`；按 .NET `ProcessStartInfo.EnvironmentVariables` 的默认继承行为，父进程已有的 `CODEX_PROXY_API_KEY` 会保留给子进程。来源：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`。
- `C:\Users\apple\.codex-exec\config.toml` 存在。来源：`Get-Item C:\Users\apple\.codex-exec\config.toml`。
- `C:\Users\apple\.codex-exec\config.toml` 中确认：
  - `model_provider = "codex-proxy"`
  - `[model_providers.codex-proxy]`
  - `base_url = "http://127.0.0.1:8080/v1"`
  - `env_key = "CODEX_PROXY_API_KEY"`
  - `wire_api = "responses"`
  来源：`Select-String` 只抽取非敏感字段。
- 当前 shell 中 `CODEX_PROXY_API_KEY=set`，未输出完整值。来源：PowerShell 环境变量检查。
- `curl` 等价检查 `Invoke-RestMethod http://127.0.0.1:8080/health` 返回：`status=ok`、`authenticated=true`、`pool.total=9`、`pool.active=9`。来源：本地 health endpoint。
- 根目录一级项包含 `heybox`、`qm-run-demo`、`QuantProject`、`sm2-randomizer`、`subtitle_extractor`，本轮未移动。来源：`Get-ChildItem -Force -Name`。
- 本次报告生成前 `.workflow/`、`.workflow/tasks/`、`.workflow/state/`、`.workflow/reports/` 均不存在。来源：`Test-Path` 检查。
- `.gitignore` 没有 `.workflow` 相关条目。来源：`Select-String -LiteralPath .gitignore -Pattern '\.workflow|workflow'` 无输出。
- 仓库中未找到 `codex-proxy-technical-path.md` 或 `codex-proxy-vs-maintenance.md`。来源：显式路径检查。
- 仓库中存在 `docs\workflows\06-codex-proxy-policy.md`。来源：`rg --files -g '*codex-proxy*'`。

## 3. Missing / Inconsistent Items

- `cx-exec.ps1` 是否 mock：
  - 根目录 `C:\Users\apple\claudecode\cx-exec.ps1` 是 mock/smoke 脚本，不调用 `codex exec`，只写死生成 `docs/workflows/99-pipeline-smoke-test.md` 和 `CODEX_RESULT.md`。
  - `C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1` 不是纯 mock，会调用 `codex exec`，但仍会在外层写 `CODEX_RESULT.md`。
- 两个 `cx-exec.ps1` 是否一致：
  - 不一致，hash 不同，行为也不同。根目录版本会直接写 smoke test；`scripts/workflow` 版本会尝试真实 codex。
- wrapper 路径是否和方案一致：
  - C# wrapper 当前真实位置是 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs/.exe`。
  - 仓库内 `scripts/workflow/codex-exec-wrapper.cs/.exe` 不存在。
  - 如果方案期望仓库内 wrapper，则当前不一致；如果方案期望 VS Code 使用用户级 wrapper，则当前路径一致。
- CODEX_HOME 是否一致：
  - C# wrapper 固定 `C:\Users\apple\.codex-exec`。
  - `scripts/workflow/cx-exec.ps1` 默认 `.codex-exec-$USERNAME`，当前用户下会是相对路径 `.codex-exec-apple`。
  - 根目录 mock 默认 `.codex-exec-cc`。
  - 三者不一致。
- `CODEX_PROXY_API_KEY` 是否进入子进程：
  - 当前 shell 中变量为 set。
  - C# wrapper 源码未删除该变量，理论上会继承给真实 Codex 子进程。
  - `scripts/workflow/cx-exec.ps1` 也未删除该变量，调用 `& codex exec` 时通常会继承当前 PowerShell 环境。
  - 但本轮未执行真实 `codex exec`，未用子进程回显环境做动态证明。
- codex-proxy 配置是否真实生效：
  - `C:\Users\apple\.codex-exec\config.toml` 配置存在且字段正确。
  - 只有当实际执行入口使用 `CODEX_HOME=C:\Users\apple\.codex-exec` 时，这份配置才会被读取。
  - 当前 `scripts/workflow/cx-exec.ps1` 默认不会使用这个绝对 `CODEX_HOME`，因此默认不保证生效。
  - `docs\workflows\06-codex-proxy-policy.md` 已记录：CLI banner 显示 provider 不等于 proxy 路由验收，且此前 `codex exec` 不应写成已验收的 proxy 执行通道。
- 401 是否能复现：
  - 本轮未复现 401，因为没有执行真实 `codex exec`。
  - 根目录 mock `cx-exec.ps1` 不调用 codex，所以 401 不可能由当前根目录 `cx-exec.ps1` 直接产生。
  - 如果 `scripts/workflow/cx-exec.ps1` 真实运行后出现 401，优先排查：默认 `CODEX_HOME` 未指向 `C:\Users\apple\.codex-exec`；其次排查 `CODEX_PROXY_API_KEY` 是否在调用进程中 set；再排查当前 PATH 上的 npm `codex.ps1` 与 C# wrapper 指向的 Codex exe 版本/行为差异。
  - 当前 health endpoint 已认证且 pool active，不支持“本地 proxy health 未认证”这个 401 解释。

## 4. Current File Map

- `C:\Users\apple\claudecode\cx-exec.ps1`
  - 根目录 mock/smoke 脚本。
  - 不调用 `codex exec`。
  - 会写 `docs/workflows/99-pipeline-smoke-test.md` 和 `CODEX_RESULT.md`。
  - 不应作为 CC -> Codex 真实执行链路入口。

- `C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1`
  - workflow 层 PowerShell 调用器。
  - 会运行 health/env 诊断。
  - 通过 PATH 调用 `codex exec "$TaskDescription"`。
  - 当前默认 `CODEX_HOME=.codex-exec-$USERNAME`，与 wrapper 的 `C:\Users\apple\.codex-exec` 不一致。
  - 当前 PATH 上 `codex` 是 `C:\Users\apple\AppData\Roaming\npm\codex.ps1`，版本 `0.121.0`。

- `C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`
  - VS Code Codex 专用 C# wrapper 源码。
  - 启动 `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`。
  - 设置 `CODEX_HOME=C:\Users\apple\.codex-exec`。
  - 不写全局环境变量，不读取或修改认证文件。
  - 未删除 `CODEX_PROXY_API_KEY`，父进程已有时应继承。

- `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
  - C# wrapper 可执行文件。
  - `--version` 返回 `codex-cli 0.130.0-alpha.5`。

- `C:\Users\apple\claudecode\scripts\workflow\codex-exec-wrapper.cs`
  - 不存在。

- `C:\Users\apple\claudecode\scripts\workflow\codex-exec-wrapper.exe`
  - 不存在。

- `C:\Users\apple\.codex-exec\config.toml`
  - 存在。
  - 包含 `codex-proxy` provider 配置。
  - 非敏感字段确认：`base_url=http://127.0.0.1:8080/v1`、`env_key=CODEX_PROXY_API_KEY`、`wire_api=responses`。

- `codex-proxy-technical-path.md`
  - 未在 `C:\Users\apple\claudecode\` 或 `C:\Users\apple\claudecode\docs\` 找到。

- `codex-proxy-vs-maintenance.md`
  - 未在 `C:\Users\apple\claudecode\` 或 `C:\Users\apple\claudecode\docs\` 找到。

- `C:\Users\apple\claudecode\docs\workflows\06-codex-proxy-policy.md`
  - 实际存在的 proxy 口径文档。
  - 记录当前不应把 VS Code Codex 插件或 `codex exec` 写成已验收 proxy 执行通道。

## 5. Root Directory Cleanup Candidates

- 不移动 `heybox` / `qm-run-demo` / `QuantProject` / `sm2-randomizer` / `subtitle_extractor`。它们按用户说明是独立仓库拉下来看，本轮只确认存在。
- `README.md` 和 `PROJECT.md` 都保留：
  - `README.md` 给人读。
  - `PROJECT.md` 给 agent 读。
- 根目录临时产物建议后续迁入 `.workflow/`，本轮不执行移动：
  - `CODEX_RESULT.md`
  - `CLAUDE_REVIEW.md`
  - `TASK_HANDOFF.md`
  - `diagnosis.log`
  - `.task-worktree.json`
- `.workflow/` 建议成为后续 workflow 状态、任务 handoff、诊断报告和执行结果的集中位置。
- `.gitignore` 当前没有 `.workflow` 规则；后续若迁入状态产物，应单独决定哪些跟踪、哪些忽略。

## 6. Proposed Next Command

```powershell
codex exec "Rewrite C:\Users\apple\claudecode\cx-exec.ps1 from the current mock/smoke script into a real Codex caller that delegates to C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1, uses CODEX_HOME=C:\Users\apple\.codex-exec by default, does not print secrets, and preserves README.md, PROJECT.md, AGENTS.md, CLAUDE.md, and the five independent repo directories."
```
