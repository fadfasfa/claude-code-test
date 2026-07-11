# Hextech 伴生系统运行指南

主项目文档位于当前目录的 [PROJECT.md](PROJECT.md)。本文只回答“这个工作区现在是什么、怎么启动、怎么打包、怎么验收”。

## 当前定位

`run/` 是 Hextech 伴生系统的实际运行工作区，包含桌面悬浮窗、本地 Web/API、阶段 0-5 游戏内 overlay 基础窗口、Vision sidecar、数据处理、远端抓取、自愈修复和 PyInstaller 便携包构建。

当前项目目标：让打包后的便携目录在非仓库、空运行态目录中首次启动后 60 秒内可用；高频抓取数据不随包分发，由首次启动和 4 小时新鲜度策略触发后台刷新。

当前游戏内显示已完成阶段 0-5 的本地 MVP：overlay host 默认不显示占位框；显示条件为“开关开 + active 海克斯选择事件 + 游戏窗口在前台”；Vision sidecar 先用蓝色选择按钮做场景门控，再通过本地事件文件刷新三槽位；ServiceManager 可同时管理 overlay host 与常驻 Vision sidecar；打包边界和性能记录结构已纳入自检。真实 LoL `Borderless` / 无边框全屏下的识别置信度、`Alt+H`、窗口跟随和 P95 <= 500ms 仍需人工验收。

## 一眼看懂

| 维度 | 当前状态 |
| :--- | :--- |
| 源码态 Python | 固定使用 `run/.venv` 内 Python 3.11；入口误用系统 Python 时会在 `.venv` 缺失时用 `py -3.11` 自动创建，最终只切回 `.venv` |
| 主入口 | `.\.venv\Scripts\python.exe hextech_ui.py` 启动桌面伴生；`.\.venv\Scripts\python.exe web_server.py` 只启动 Web 服务；`.\.venv\Scripts\python.exe hextech_ui.py --game-overlay` 只启动基础 overlay host |
| 打包入口 | `.\.venv\Scripts\python.exe build.py`，不要使用裸系统 Python 打包 |
| 发布形态 | PyInstaller `--onedir` 未签名便携包 + zip，输出到仓库根 `.artifacts/hextech/releases/` |
| 启动硬门槛 | 打包产物空仓首启 60 秒内返回可用 Web/UI 热路径 |
| 高频数据策略 | `data/runtime/raw/` 和其他 `data/runtime/` 运行态目录不进包；首次空仓必刷，之后超过 4 小时再刷 |
| 稳定资源策略 | 源码态唯一数据根是 `data/`；只把 `data/static/**` 和 `data/seed/startup/**` 的稳定输入放进包，不使用运行态 raw |
| 最近验收 | `.\.venv\Scripts\python.exe tools/acceptance/smoke_packaged_startup.py --timeout 60`，严格空仓实测约 3.83 秒可用 |

## 快速命令

```powershell
# 手动创建/修复稳定 venv，并安装依赖；源码入口在 .venv 缺失时会自动执行同等 bootstrap
py -3.11 tools/setup_venv.py

# 若只需补依赖，也必须装入 run/.venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 桌面伴生模式
.\.venv\Scripts\python.exe hextech_ui.py

# 仅启动 Web 服务
.\.venv\Scripts\python.exe web_server.py

# overlay host 人工验收入口；无 active 选择事件或游戏不在前台时窗口保持隐藏
.\.venv\Scripts\python.exe hextech_ui.py --game-overlay

# 写入一条本地三槽位样例事件；仍需游戏窗口在前台才会显示
.\.venv\Scripts\python.exe -c "from hextech.overlay.events import write_sample_overlay_event; print(write_sample_overlay_event())"

# 阶段 3 假识别：写真实事件文件，验证事件通道到三槽位渲染
.\.venv\Scripts\python.exe -c "from hextech.overlay.events import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"

# 阶段 3R Vision MVP：执行一次本地窗口识别探针并写入事件文件
.\.venv\Scripts\python.exe -m hextech.overlay.vision.sidecar --once --preset auto --write-event

# 正式游戏内显示链路：常驻自门控识别循环
.\.venv\Scripts\python.exe -m hextech.overlay.vision.sidecar --loop --preset auto --write-event

# 识别校准：在真实卡片界面转储单帧、ROI crop 与 top3 候选分数到目录
.\.venv\Scripts\python.exe -m hextech.overlay.vision.sidecar --once --debug-dump data/runtime/debug/overlay_vision

# 官方接口优先：只读探测 Riot / LoL 本地接口是否提供三槽候选
.\.venv\Scripts\python.exe tools/acceptance/probe_official_overlay_provider.py --duration-seconds 120 --interval-ms 500 --dump-runtime-json

# 阶段 5 性能验收摘要：手工录入延迟样本后输出 P50/P95
.\.venv\Scripts\python.exe tools/acceptance/overlay_performance_probe.py --latency-ms 180 240 420 --source-tag manual-lol-borderless

# 写入非选择态事件，overlay 应隐藏
.\.venv\Scripts\python.exe -c "from hextech.overlay.events import write_inactive_overlay_event; print(write_inactive_overlay_event())"

# 默认离线自检
.\.venv\Scripts\python.exe tools/dev_checks.py

# 手动重抓 Mayhem combo raw；只使用 Scrapling 普通 GET
.\.venv\Scripts\python.exe -m hextech.scraping.synergy.mayhem_combo_scraper --max-pages 1 --output data/evidence/mayhem_combos.raw.json

# 将 Mayhem raw 增量合并到前端优先读取的 cleaned 协同数据
.\.venv\Scripts\python.exe tools/clean_mayhem_combos.py

# 打包便携产物
.\.venv\Scripts\python.exe build.py

# 打包资源白名单明细校验
.\.venv\Scripts\python.exe tools/dev_checks.py --bundle-manifest

# 打包后空仓首启验收
.\.venv\Scripts\python.exe tools/acceptance/smoke_packaged_startup.py --timeout 60

# Web/UI 详情页联动手动验收辅助，需先启动本地 Web 服务
.\.venv\Scripts\python.exe tools/dev_checks.py --manual-web-synergy --base-url http://127.0.0.1:8000
```

## 目录职责

```text
run/
├── build.py                    # 打包入口薄壳，委托 tools/build_package.py
├── hextech_ui.py               # 桌面入口薄壳，委托 hextech.display.desktop
├── web_server.py               # Web 入口薄壳，委托 hextech.display.web
├── hextech/                    # 主应用包；所有可 import 业务代码收口到这里
│   ├── core/                   # 刷新编排、运行态偏好和轻量核心合同
│   ├── catalog/                # CSV/DataFrame、预计算缓存、别名检索、视图适配
│   ├── display/desktop/        # Tk 桌面 UI、后台运行时与服务生命周期
│   ├── display/web/            # FastAPI、本地 API、Web 运行时和编译静态页面
│   ├── overlay/                # 游戏内显示 host、renderer、事件、provider、vision
│   ├── scraping/               # 业务抓取、稳定资源同步、自愈与底层 transport
│   └── support/                # run/.venv 守卫、原子写入、日志等跨域基础工具
├── frontend/                   # Tailwind 源码与 Node 构建配置
├── tools/                      # 打包、兼容自检入口、手动验收和烟测工具
│   ├── build_package.py        # 唯一打包脚本；临时构建目录写入系统 TEMP
│   ├── package_rules.py        # 打包资源规则；只描述源路径，不复制资源
│   ├── dev_checks.py           # pytest 门禁兼容 CLI 与非 pytest 辅助模式
│   └── acceptance/             # 验收工具入口
├── data/                       # 源码态唯一数据根；稳定数据、首启 seed、证据、fixture 和 runtime 分区
│   ├── static/version/         # 版本级稳定数据与索引文件
│   ├── static/assets/          # 稳定图片/图标资源入口
│   ├── seed/startup/           # 构建期首启种子输入
│   ├── evidence/               # 可审计来源证据，不直接供 UI 消费
│   ├── fixtures/diagnostics/   # 离线诊断样例和真值
│   └── runtime/                # 本机运行态状态、缓存、日志、锁和 profile
├── docs/                       # 业务设计和审查文档
└── tests/                      # 唯一自动化测试事实源；含分域 pytest 回归与开发门禁
```

更细的文件职责、数据流和维护边界见 [PROJECT.md](PROJECT.md)。

## 数据目录分类

`data/` 是源码态唯一数据根，`resources/` 不再作为真实路径存在。兼容 Web URL 仍保持：

- `/assets/...`：对外图片 URL，源码态文件来自 `data/static/assets/`。
- `/data/static/...` 与 `/data/indexes/...`：对外稳定数据兼容路由，源码态文件来自 `data/static/version/`。
- `data/seed/startup/`：构建期首启 seed 输入。
- `data/evidence/mayhem_combos.raw.json`：来源证据，用于复现 cleaned 协同数据。
- `data/runtime/**`：运行态输出，不作为稳定资源。

分类清单见 `data/data_manifest.v1.json`。目录索引见 `data/README.md`，具体用途、消费方、
写入规则和打包边界见 `data/DATA_USAGE.md`。

## 运行态数据边界

### 可以随包分发

- `hextech/display/web/static/` 前端静态页面
- `data/static/version/` 中的版本级稳定数据和索引文件
- `data/static/assets/` 中的稳定图片/图标资源
- `data/seed/startup/` 中的构建期首启 seed 输入
- `英雄目录.v1.json`
- `海克斯资源目录.v1.json`
- `Champion_Synergy_Cleaned.json`
- `hero_version.txt`

旧 `/data/static/...` 与 `/data/indexes/...` 文件名由 API 投影兼容，不作为包内实体事实源。
`Champion_Synergy_Cleaned.json` 是协同展示的清洗后静态事实源；Web/API 与
overlay hint cache 默认通过统一运行路径读取它，只有缺失时才回退启动后的 raw latest
或旧固定名快照。

### 不应随包分发

- `data/runtime/raw/hextech/Hextech_Data_*.csv`
- 启动后新生成的 `data/runtime/raw/synergy/Champion_Synergy_*.json`
- 启动后新生成的 `data/runtime/raw/synergy/Champion_Synergy_latest.v1.json`
- 迁移前旧路径 `data/raw/**`
- `data/runtime/state/*.json`
- `data/runtime/state/web_server_port.txt`
- `data/runtime/cache/`
- `data/runtime/locks/`
- `data/runtime/profile/`
- `data/runtime/logs/`
- 任何启动后生成、抓取、缓存、锁、日志或计算产物

例外：`data/evidence/mayhem_combos.raw.json` 是显式入库的 Mayhem 清洗输入，用于复现
`Champion_Synergy_Cleaned.json` 的生成；它不进入打包白名单，也不被 API 或前端直接读取。
重抓 raw 后只有在同步更新 cleaned 数据时才一起提交。

打包只允许带一组首启冷启动种子，但包内路径必须是
`data/seed/startup/hextech/` 与 `data/seed/startup/synergy/`。启动时再由
`tools/runtime_bundle.py` 播种到用户运行目录的 `raw/*`；源码态与冻结态都对应
各自运行根下的 `data/runtime/raw/`。
旧固定名
`Champion_Synergy.json` 仅作读取兜底，不再作为刷新成功或最新数据判断依据。

### 首启会自动创建

运行态骨架包含：

- `raw/hextech/`
- `raw/synergy/`
- `state/`
- `cache/`
- `locks/`
- `profile/`
- `persisted/`
- `logs/`

源码态根目录是 `run/data/runtime/`；冻结态根目录是
`%LOCALAPPDATA%/HextechNexus/data/runtime/`，且不接受 `HEXTECH_BASE_DIR`
把运行态覆盖回便携包根。

### 日志与诊断导出

运行态日志采用双层体系：

- 源码态默认 `HEXTECH_LOG_PROFILE=dev`，写入 `data/runtime/logs/dev/hextech_full.jsonl`、`hextech_runtime_summary.log` 和 `hextech_error.log`。full JSONL 用于开发排障，包含 `run_id`、`correlation_id`、组件、事件、线程、进程、耗时和脱敏后的异常字段，并按 10MB x 5 轮转。
- 冻结态默认 `packaged`，不写 full debug JSONL，只保留摘要、错误日志和 `state/*.jsonl` 事件尾部，供用户轻量导出。
- 桌面标题栏右侧的 `诊断` 按钮只导出 `data/runtime/reports/user_diagnostics/<timestamp>.zip`，内容为白名单 state、日志/事件 tail、`summary.json` 和说明文件。
- 用户导出包不会采集 `debug/`、旧 `reports/`、`raw/`、`cache/`、`profile/`，也会跳过或脱敏 `auth/token/cookie/secret/nonce/lcu/riot` 相关文件和内容。

开发侧重型采集仍使用：

```powershell
.\.venv\Scripts\python.exe tools/collect_runtime_diagnostics.py
```

该工具不进入发布包；打包态只使用 `hextech/support/user_diagnostics.py` 中的轻量导出逻辑。

## 打包与验收

pytest 是自动化测试的唯一事实源。开发阶段可直接运行 pytest；原有命令继续作为
兼容 CLI，根据 marker 委托给 pytest：

```powershell
# 完整自动化测试
.\.venv\Scripts\python.exe -m pytest -q

# 默认开发门禁：pytest -m "dev_gate and not deep"
.\.venv\Scripts\python.exe tools/dev_checks.py

# 仅 overlay fast 门禁：pytest -m "dev_gate and overlay and not deep"
.\.venv\Scripts\python.exe tools/dev_checks.py --overlay-only

# 全部分域深度门禁：pytest -m "dev_gate"
.\.venv\Scripts\python.exe tools/dev_checks.py --deep

# 仅 overlay 深度门禁：pytest -m "dev_gate and overlay"
.\.venv\Scripts\python.exe tools/dev_checks.py --overlay-only --deep
```

`tools/dev_checks.py` 不保存自动化断言或检查函数注册表，只保留旧命令的参数兼容、
pytest 退出码透传，以及 bundle manifest、海克斯健康摘要和 Web 联动人工验收等
非 pytest 模式。

如需查看打包资源白名单明细：

```powershell
.\.venv\Scripts\python.exe tools/dev_checks.py --bundle-manifest
```

只读查看海克斯胜率/联动抓取健康摘要：

```powershell
.\.venv\Scripts\python.exe tools/dev_checks.py --hextech-health
```

`.\.venv\Scripts\python.exe build.py` 会生成：

- `.artifacts/hextech/releases/HextechCompanion-YYYYMMDD/`
- `.artifacts/hextech/releases/HextechCompanion-YYYYMMDD.zip`
- 便携目录内的 `Hextech伴生终端.exe`
- 便携目录内的 `启动 Hextech.bat`
- 便携目录内的 `README_首次使用.txt`

构建过程使用系统临时目录 `hextech-build-*`，不会在 `run/build/`、`run/dist/`
或 `run/build/_bundle_runtime/` 保留资源副本。旧目录若存在只视为历史生成物，
可由新打包入口在构建前清理。

发布前建议固定执行：

```powershell
.\.venv\Scripts\python.exe tools/acceptance/smoke_packaged_startup.py --timeout 60
```

这个烟测会复制最新打包目录，使用隔离的 `LOCALAPPDATA` 启动 exe，且不预删复制品中的
forbidden 目录。它会检查：

- 冻结态运行态是否创建在 `HextechNexus/data/runtime`
- `web_server_port.txt` 是否新写入
- `startup_status.json` 是否新写入
- 包根与 `_internal` 下是否不存在 `data/raw`、`data/runtime`、`data/processed` 和运行期 cache/profile/log/logs/debug
- `/`、`/api/startup_status`、`/api/champions`、`/detail.html?champion=1`、`/api/synergies/1` 是否可访问

Web/UI 详情页右侧联动对齐 ApexLoL 源页的检查保留为手动验收辅助，
因为它依赖浏览器、本地 Web 服务和外网：

```powershell
.\.venv\Scripts\python.exe tools/dev_checks.py --manual-web-synergy --base-url http://127.0.0.1:8000
```

阶段 0-5 游戏内显示验收覆盖基础窗口、本地事件通道、Vision MVP、生命周期和性能/打包边界：

- 无事件文件、inactive 事件、过期事件、客户端阶段、桌面前台和游戏切后台时，overlay 默认不显示占位框。
- overlay 显示条件为“开关开 + active 海克斯选择事件 + 游戏窗口在前台”；`Alt+H` 只切换用户开关，不绕过事件和前台门控。
- 蓝色选择按钮是 Vision sidecar 的主场景门控：新环境首次定位并写入 `data/runtime/state/overlay_anchor_calibration.v1.json`，后续每轮仍检测固定按钮 ROI；按钮不存在或锻体碎片三选一时 overlay 隐藏。
- 在 LoL `Borderless` / 无边框全屏下确认 overlay 可见、置顶、点击穿透、`Alt+H` 显隐、选择结束自动隐藏和跟随游戏窗口。
- ROI 预设覆盖 `1920x1080`、`2560x1440` 和重点 `2560x1600`；DPI 缩放、多显示器和分辨率切换仍需人工记录。
- 在桌面控制台分别验证只开 Web、只开游戏内显示、两者同开、两者全关四种矩阵。
- 只开游戏内显示时不得出现 FastAPI 端口、浏览器进程或 Web API 依赖。
- 关闭游戏内显示后不得残留 overlay、Vision sidecar、高频捕获或识别循环；低频监听保持内部默认策略，可计量但不再占用悬浮窗开关位。
- 当前 MVP 只承诺 `Borderless` / 无边框全屏；`Full Screen` / 独占全屏不承诺独占全屏覆盖，后续只做检测与引导。
- 真实 LoL 人工验收需记录 P95 <= 500ms 的 overlay 文案更新延迟；识别输出目标 P95 <= 300ms。

下一截断的数据通道验收只覆盖本地 JSON 事件：

- `data/runtime/state/game_overlay_slots.v1.json` 是 overlay host 读取的本地三槽位事件文件。
- `.\.venv\Scripts\python.exe -c "from hextech.overlay.events import write_sample_overlay_event; print(write_sample_overlay_event())"` 可写入开发样例事件。
- `.\.venv\Scripts\python.exe -c "from hextech.overlay.events import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"` 可写入假识别事件，用于先验证“事件文件 -> overlay 三槽位渲染”的端到端通道。
- `.\.venv\Scripts\python.exe -m hextech.overlay.vision.sidecar --once --preset auto --write-event` 可执行一次本地 Vision 诊断探针；无 LoL 窗口时会写入 inactive 诊断事件。
- `.\.venv\Scripts\python.exe -m hextech.overlay.vision.sidecar --loop --preset auto --write-event` 是正式常驻链路；游戏窗口不存在或不在前台时低频待机，并只写一次 inactive 清理旧 active 事件；前台时按约 250ms 截图识别。
- 识别判据为蓝色按钮 ROI 场景门控 + 灰度归一化指纹（NCC）+ top1/top2 区分度 margin + crop 方差下限：平坦暗面板（如 ESC 菜单）会被方差门槛直接拒绝，不会误报 active；模板按图标内容去重，近孪生图标在置信度极高时豁免 margin；active 掉到 unstable 后会延迟约 3 帧再写隐藏事件，避免 overlay 闪烁。
- ROI 直接框住三张卡片的图标区；`2560x1600` 来自真实截图标定，16:9 预设为推算值。识别不准时在真实卡片界面运行 `--once --debug-dump <目录>`，依据转储的 `report.json`（各槽 crop_std 与 top3 置信度）校准 ROI 与阈值。
- `selection_type=hextech` 是唯一可显示选择态；`body_shard` 只保留为诊断类型，锻体碎片选择写 `body_shard_only` 并隐藏 overlay。
- 没有海克斯选择、蓝色按钮不存在或事件 inactive/缺失/过期时，overlay host 隐藏，不显示等待占位。
- overlay host 应在不启动 Web/FastAPI/浏览器的情况下刷新三槽位文案。
- Vision sidecar 使用 Pillow/pywin32 本地窗口截图和固定 ROI 预设，不引入 OpenCV、WGC、远端请求、注入、读内存或自动点击。

## 常用接口

- `GET /api/champions`：英雄列表
- `GET /api/champion/{name}/hextechs`：英雄海克斯推荐
- `GET /api/champion_aliases`：首页搜索专用英雄别名索引
- `GET /api/augment_icon_map`：海克斯图标映射
- `GET /api/live_state`：当前 LCU 英雄选择状态；`champion_ids` 保持兼容，`selected_champion_ids` / `bench_champion_ids` 供桌面悬浮窗排序
- `GET /api/synergies/{champ_id}`：英雄协同数据
- `POST /api/redirect`：浏览器跳转控制
- `GET /ws`：实时事件推送
- `hextech/`：主应用 import 根，承载 desktop/web/catalog/overlay/scraping/core 主实现
- `data/runtime/cache/overlay_hint_cache.v1.json`：游戏内 overlay 本地轻量提示缓存
- `hextech/catalog/runtime_store.py`：CSV 与运行时文件定位、DataFrame 缓存与归一
- `hextech/catalog/view_adapter.py`：首页榜单与海克斯详情数据适配
- `hextech/catalog/precomputed_cache.py`：预计算 API 缓存读写
- `data/runtime/state/game_overlay_slots.v1.json`：游戏内 overlay 本地三槽位事件
- `hextech/overlay/events.py`：游戏内 overlay 本地三槽位事件协议
- `hextech/overlay/hints.py`：游戏内 overlay 本地轻量提示缓存生成与查询
- `hextech/overlay/vision/sidecar.py`：游戏内 overlay 本地 Vision MVP 探针入口
- `hextech/overlay/`：独立游戏内显示产品模块
- `.\.venv\Scripts\python.exe -m hextech.overlay --self-check`：独立模块只读自检
- `tools/acceptance/overlay_performance_probe.py`：阶段 5 性能验收摘要工具

## 维护入口

- Web 路由优先改 `hextech/display/web/api.py`
- Web 生命周期、端口、浏览器、LCU、缓存逻辑优先改 `hextech/display/web/runtime.py`
- Web 前端生命周期与两个产品开关的委托优先改 `hextech/display/desktop/service_manager.py`
- 游戏内显示统一生命周期优先改 `hextech/overlay/lifecycle.py`
- 窗口、显隐和热键优先改 `hextech/overlay/host.py`
- 三统计窗、联动列和原生视觉 token 优先改 `hextech/overlay/renderer.py`
- 共享数据读取边界优先改 `hextech/overlay/data_source.py`
- 桌面后台线程、轮询、跳转、资源加载逻辑优先改 `hextech/display/desktop/runtime.py`
- 桌面控件结构优先改 `hextech/display/desktop/app.py`
- overlay hint cache 生成和查询优先改 `hextech/overlay/hints.py`
- overlay 三槽位事件通道优先改 `hextech/overlay/events.py`
- overlay Vision 探针优先改 `hextech/overlay/vision/sidecar.py`
- overlay 性能记录结构优先改 `tools/acceptance/overlay_performance_probe.py`
- 纯数据转换、DataFrame 清洗、视图适配优先改 `hextech/catalog/`
- 底层抓取 transport 优先改 `hextech/scraping/transport/`
- Scrapling 冒烟脚本优先改 `hextech/scraping/transport/smoke_scrapling.py`
- 海克斯/协同业务抓取优先改 `hextech/scraping/hextech/scraper.py` 和 `hextech/scraping/synergy/scraper.py`
- 稳定资源同步、图标目录维护、自愈修复优先改 `hextech/scraping/`
- 打包链路变更时同步检查 `tools/build_package.py`、`tools/package_rules.py`、`tools/bundle_manifest.py`、`tools/runtime_bundle.py`、`tools/dev_checks.py` 和本文档
