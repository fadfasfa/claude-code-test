# scripts

本目录放仓库级辅助脚本。旧 `scripts/workflow/` 主流程已移除；`scripts/git/` 是 legacy/manual 层，不自动触发。

| 路径 | 状态 | 用途 |
| :--- | :--- | :--- |
| `scripts/git/` | legacy/manual | 旧 Git / worktree 查看与清理辅助脚本，只能显式手动调用 |
| `scripts/scraping/apex_snapshot_capture.py` | manual | 低频手动打开 ApexLoL 入口页，保存同源 HTML/JSON/JS/text snapshot 供离线解析 |

不维护新的复杂编排器。新增脚本必须有明确用途、输入输出、写入行为和失败行为；不要读取凭据文件或默认执行发布动作。

当前没有 active AI worker wrapper；退役的 cc-worker 材料保存在 `docs/archive/cc-worker/`，不作为默认脚本入口。

## scripts/scraping/apex_snapshot_capture.py

- 输入：固定入口 `https://apexlol.info/zh` 与 `https://apexlol.info/zh/hextech`，可用 `--snapshot-dir` 指定输出目录，`--max-attempts` / `--retry-delay-seconds` 调整暂态失败重试。
- 输出：默认写入 `run/data/runtime/cache/apex_snapshot/manual/`，包含页面 HTML、同源文本响应和 `capture_manifest.json`。
- 写入行为：只写 snapshot 缓存目录，不写 `run/data/raw/synergy/`，不发布、不提交、不清理旧数据。
- 失败行为：浏览器启动和页面访问会做有限重试；页面最终失败时尽量保存当前 HTML 并在 manifest 记录失败与尝试次数；无法读取的响应会跳过。
- 安全边界：不使用代理、stealth、验证码处理或 Cloudflare challenge 处理；使用非持久浏览器上下文，不保存 cookies/storage。
