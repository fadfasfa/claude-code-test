# run/data 单一数据根分类说明

本文件记录旧稳定资源分类方案的替代口径。当前事实源已经收口到 `run/data`，
不再保留独立资源目录或镜像副本。

## 当前分类

- `data/static/version/`：版本级稳定 JSON/TXT，供 Web/API、overlay、清洗脚本和打包 manifest 读取。
- `data/static/assets/`：随包图标、图片和视觉识别稳定资源。
- `data/seed/startup/`：构建期首启 seed，打包后用于空仓首次启动播种运行态。
- `data/evidence/`：外部来源证据，只用于复现 cleaned 数据，不直接供 UI 消费。
- `data/fixtures/diagnostics/`：离线诊断样例和视觉回归真值。
- `data/runtime/`：本机运行态 raw/state/cache/logs/reports/debug/profile/locks/persisted。

## 维护规则

- 新增稳定数据先判断消费方和写入方，再选择分类；不要按旧文件名惯性放置。
- Web 与 overlay 的展示真相源是 `data/static/version/Champion_Synergy_Cleaned.json`。
- 运行态抓取、缓存、日志、profile、debug 和 report 不进入打包白名单。
- 首启 seed 可以进包，但包内目标路径保持 `data/seed/startup/...`。
- 详细用途说明以 `data/DATA_USAGE.md` 为准；机器可读分类以 `data/data_manifest.v1.json` 为准。
