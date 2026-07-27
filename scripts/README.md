# scripts

本目录放仓库级辅助脚本。旧 `scripts/workflow/` 主流程已移除；`scripts/Git辅助/` 是 legacy/manual 层，不自动触发。

| 路径 | 状态 | 用途 |
| :--- | :--- | :--- |
| `scripts/Git辅助/` | legacy/manual | 旧 Git / worktree 查看与清理辅助脚本，只能显式手动调用 |
| `scripts/抓取快照/apex_snapshot_capture.py` | manual | 低频手动打开 ApexLoL 入口页，保存同源 HTML/JSON/JS/text snapshot 供离线解析 |
| `scripts/前端样图/ui_mockup_mcp.py` | active-tooling | Codex/Claude Code 共用的本地 MCP；把自包含 HTML/CSS 候选方案渲染为对话内样图或临时文件 |
| `scripts/自检测试/` | test | 仓库级回归测试；验证规则文本和手动脚本的最小行为 |

不维护新的复杂编排器。新增脚本必须有明确用途、输入输出、写入行为和失败行为；不要读取凭据文件或默认执行发布动作。

当前没有 active AI worker wrapper；退役的 cc-worker 材料保存在 `docs/历史归档/cc-worker/`，不作为默认脚本入口。

## scripts/前端样图/ui_mockup_mcp.py

- 输入：1–3 个带 `A/B/C` 编号的自包含 HTML/CSS 方案，以及受限的 viewport 和输出模式。
- 输出：多模态工具返回 MCP 文本摘要与一张内存 JPEG；文本模型兼容工具返回系统临时目录内 JPEG 的 Markdown 路径。
- 写入行为：不写业务仓库、不持久化 HTML；仅文本模型兼容工具在 `%TEMP%/ui-mockup-renderer/` 写入随机命名 JPEG，依赖由同目录 PEP 723 lock 固定并缓存到 `uv` 环境。
- Plan Mode 语义：临时 JPEG 仅作为工具自管、不可变的输出载体；工具不读取、覆盖或修改用户/项目数据，因此按 MCP 风险提示声明为只读、非破坏性，但不把“会新增临时输出文件”的事实隐藏起来。
- 失败行为：在启动浏览器前拒绝脚本、事件处理器、外链、本地文件 URL、超限 HTML 和非法尺寸，并指出具体候选编号。
- 安全边界：使用无持久浏览器上下文，关闭 JavaScript、service worker 和全部网络请求；只可能读取已知 OS 字体以显示中文标签，不读取凭据、cookie 或项目文件。

## scripts/抓取快照/apex_snapshot_capture.py

- 输入：固定入口 `https://apexlol.info/zh` 与 `https://apexlol.info/zh/hextech`，可用 `--snapshot-dir` 指定输出目录，`--max-attempts` / `--retry-delay-seconds` 调整暂态失败重试。
- 输出：默认写入 `run/data/runtime/cache/apex_snapshot/manual/`，包含页面 HTML、同源文本响应和 `capture_manifest.json`。
- 写入行为：只写 snapshot 缓存目录，不写 `run/data/raw/synergy/`，不发布、不提交、不清理旧数据。
- 失败行为：浏览器启动和页面访问会做有限重试；页面最终失败时尽量保存当前 HTML 并在 manifest 记录失败与尝试次数；无法读取的响应会跳过。
- 安全边界：不使用代理、stealth、验证码处理或 Cloudflare challenge 处理；使用非持久浏览器上下文，不保存 cookies/storage。
