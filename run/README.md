# Hextech 伴生系统运行指南

主项目文档位于当前目录的 [PROJECT.md](PROJECT.md)。本文只回答“这个工作区现在是什么、怎么启动、怎么打包、怎么验收”。

## 当前定位

`run/` 是 Hextech 伴生系统的实际运行工作区，包含桌面悬浮窗、本地 Web/API、阶段 0-5 游戏内 overlay 基础窗口、Vision sidecar、数据处理、远端抓取、自愈修复和 PyInstaller 便携包构建。

当前项目目标：让打包后的便携目录在非仓库、空运行态目录中首次启动后 60 秒内可用；高频抓取数据不随包分发，由首次启动和 4 小时新鲜度策略触发后台刷新。

当前游戏内显示已完成阶段 0-5 的本地 MVP：overlay host 默认不显示占位框；显示条件为“开关开 + active 海克斯选择事件 + 游戏窗口在前台”；Vision sidecar 先用蓝色选择按钮做场景门控，再通过本地事件文件刷新三槽位；ServiceManager 可同时管理 overlay host 与常驻 Vision sidecar；打包边界和性能记录结构已纳入自检。真实 LoL `Borderless` / 无边框全屏下的识别置信度、`Alt+H`、窗口跟随和 P95 <= 500ms 仍需人工验收。

## 一眼看懂

| 维度 | 当前状态 |
| :--- | :--- |
| 主入口 | `python hextech_ui.py` 启动桌面伴生；`python web_server.py` 只启动 Web 服务；`python hextech_ui.py --game-overlay` 只启动基础 overlay host |
| 打包入口 | `python build.py`，不要另建平行打包流程 |
| 发布形态 | PyInstaller `--onedir` 未签名便携包 + `_portable.zip` |
| 启动硬门槛 | 打包产物空仓首启 60 秒内返回可用 Web/UI 热路径 |
| 高频数据策略 | `data/raw/` 和 `data/runtime/` 不进包；首次空仓必刷，之后超过 4 小时再刷 |
| 稳定资源策略 | 只把版本级稳定资源放进包；首启种子快照在包内使用 `resources/snapshots/`，不使用 `data/raw/` |
| 最近验收 | `python tools/acceptance/smoke_packaged_startup.py --timeout 60`，严格空仓实测约 3.83 秒可用 |

## 快速命令

```powershell
# 安装依赖
pip install -r requirements.txt

# 桌面伴生模式
python hextech_ui.py

# 仅启动 Web 服务
python web_server.py

# overlay host 人工验收入口；无 active 选择事件或游戏不在前台时窗口保持隐藏
python hextech_ui.py --game-overlay

# 写入一条本地三槽位样例事件；仍需游戏窗口在前台才会显示
python -c "from hextech.overlay.events import write_sample_overlay_event; print(write_sample_overlay_event())"

# 阶段 3 假识别：写真实事件文件，验证事件通道到三槽位渲染
python -c "from hextech.overlay.events import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"

# 阶段 3R Vision MVP：执行一次本地窗口识别探针并写入事件文件
python -m hextech.overlay.vision.sidecar --once --preset auto --write-event

# 正式游戏内显示链路：常驻自门控识别循环
python -m hextech.overlay.vision.sidecar --loop --preset auto --write-event

# 识别校准：在真实卡片界面转储单帧、ROI crop 与 top3 候选分数到目录
python -m hextech.overlay.vision.sidecar --once --debug-dump data/runtime/debug/overlay_vision

# 官方接口优先：只读探测 Riot / LoL 本地接口是否提供三槽候选
python tools/acceptance/probe_official_overlay_provider.py --duration-seconds 120 --interval-ms 500 --dump-runtime-json

# 阶段 5 性能验收摘要：手工录入延迟样本后输出 P50/P95
python tools/acceptance/overlay_performance_probe.py --latency-ms 180 240 420 --source-tag manual-lol-borderless

# 写入非选择态事件，overlay 应隐藏
python -c "from hextech.overlay.events import write_inactive_overlay_event; print(write_inactive_overlay_event())"

# 默认离线自检
python tools/dev_checks.py

# 打包便携产物
python build.py

# 打包资源白名单明细校验
python tools/dev_checks.py --bundle-manifest

# 打包后空仓首启验收
python tools/acceptance/smoke_packaged_startup.py --timeout 60

# Web/UI 详情页联动手动验收辅助，需先启动本地 Web 服务
python tools/dev_checks.py --manual-web-synergy --base-url http://127.0.0.1:8000
```

## 目录职责

```text
run/
├── build.py                    # 打包入口薄壳，委托 tools/build_bundle.py
├── hextech_ui.py               # 桌面入口薄壳，委托 hextech.display.desktop
├── web_server.py               # Web 入口薄壳，委托 hextech.display.web
├── hextech/                    # 主应用包；所有可 import 业务代码收口到这里
│   ├── core/                   # 刷新编排、运行态偏好和轻量核心合同
│   ├── catalog/                # CSV/DataFrame、预计算缓存、别名检索、视图适配
│   ├── display/desktop/        # Tk 桌面 UI、后台运行时与服务生命周期
│   ├── display/web/            # FastAPI、本地 API、Web 运行时和编译静态页面
│   ├── overlay/                # 游戏内显示 host、renderer、事件、provider、vision
│   ├── scraping/               # 业务抓取、稳定资源同步、自愈与底层 transport
│   └── support/                # 原子写入、日志等跨域基础工具
├── frontend/                   # Tailwind 源码与 Node 构建配置
├── tools/                      # 打包、自检、手动验收和烟测工具
│   ├── checks/                 # dev_checks 分域检查清单
│   └── acceptance/             # 验收工具入口
├── resources/                  # 稳定只读资源边界；打包快照输出到 resources/snapshots/
├── docs/                       # 业务设计和审查文档
├── data/static/                # 版本级稳定数据文件
├── data/indexes/               # 版本级稳定索引文件
├── assets/                     # 稳定图片/图标资源入口
└── data/                       # 本地运行生成数据；不作为分发源数据
```

更细的文件职责、数据流和维护边界见 [PROJECT.md](PROJECT.md)。

## 运行态数据边界

### 可以随包分发

- `hextech/display/web/static/` 前端静态页面
- `data/static/` 中的版本级稳定数据文件
- `data/indexes/` 中的版本级稳定索引文件
- `assets/` 中的稳定图片/图标资源
- `resources/snapshots/` 中的打包首启种子快照
- `Champion_Core_Data.json`
- `Champion_Alias_Index.json`
- `Augment_Icon_Manifest.json`
- 兼容图标映射文件

### 不应随包分发

- `data/raw/hextech/Hextech_Data_*.csv`
- 启动后新生成的 `data/raw/synergy/Champion_Synergy_*.json`
- 启动后新生成的 `data/raw/synergy/Champion_Synergy_latest.v1.json`
- `data/runtime/state/*.json`
- `data/runtime/state/web_server_port.txt`
- `data/runtime/cache/`
- `data/runtime/locks/`
- `data/runtime/profile/`
- `data/runtime/logs/`
- 任何启动后生成、抓取、缓存、锁、日志或计算产物

打包只允许带一组首启冷启动种子，但包内路径必须是
`resources/snapshots/hextech/` 与 `resources/snapshots/synergy/`。启动时再由
`tools/runtime_bundle.py` 播种到用户运行目录的 `data/raw/*`。旧固定名
`Champion_Synergy.json` 仅作读取兜底，不再作为刷新成功或最新数据判断依据。

### 首启会自动创建

- `data/raw/hextech/`
- `data/raw/synergy/`
- `data/runtime/state/`
- `data/runtime/cache/`
- `data/runtime/locks/`
- `data/runtime/profile/`
- `data/runtime/persisted/`
- `data/runtime/logs/`

## 打包与验收

开发阶段默认先运行统一离线自检：

```powershell
python tools/dev_checks.py
```

该入口包含结构收口、别名索引、日志契约、bundle manifest、协同数据
freshness、快照定位、发布熔断和结构化协同 payload 回归检查。`run/tests/`
不再作为独立临时测试目录保留。

如需查看打包资源白名单明细：

```powershell
python tools/dev_checks.py --bundle-manifest
```

`python build.py` 会生成：

- `dist/Hextech_伴生系统_YYYYMMDD/`
- `dist/Hextech_伴生系统_YYYYMMDD_portable.zip`
- 便携目录内的 `Hextech伴生终端.exe`
- 便携目录内的 `启动 Hextech.bat`
- 便携目录内的 `README_首次使用.txt`

发布前建议固定执行：

```powershell
python tools/acceptance/smoke_packaged_startup.py --timeout 60
```

这个烟测会复制最新打包目录，删除复制品中的 `data/raw` 和 `data/runtime`，再启动 exe 检查：

- 运行态目录是否重新创建
- `web_server_port.txt` 是否新写入
- `startup_status.json` 是否新写入
- `_internal/data/runtime` 是否不存在
- `/`、`/api/startup_status`、`/api/champions`、`/detail.html?champion=1`、`/api/synergies/1` 是否可访问

Web/UI 详情页右侧联动对齐 ApexLoL 源页的检查保留为手动验收辅助，
因为它依赖浏览器、本地 Web 服务和外网：

```powershell
python tools/dev_checks.py --manual-web-synergy --base-url http://127.0.0.1:8000
```

阶段 0-5 游戏内显示验收覆盖基础窗口、本地事件通道、Vision MVP、生命周期和性能/打包边界：

- 无事件文件、inactive 事件、过期事件、客户端阶段、桌面前台和游戏切后台时，overlay 默认不显示占位框。
- overlay 显示条件为“开关开 + active 海克斯选择事件 + 游戏窗口在前台”；`Alt+H` 只切换用户开关，不绕过事件和前台门控。
- 蓝色选择按钮是 Vision sidecar 的主场景门控：新环境首次定位并写入 `data/runtime/state/overlay_anchor_calibration.v1.json`，后续每轮仍检测固定按钮 ROI；按钮不存在或锻体碎片三选一时 overlay 隐藏。
- 在 LoL `Borderless` / 无边框全屏下确认 overlay 可见、置顶、点击穿透、`Alt+H` 显隐、选择结束自动隐藏和跟随游戏窗口。
- ROI 预设覆盖 `1920x1080`、`2560x1440` 和重点 `2560x1600`；DPI 缩放、多显示器和分辨率切换仍需人工记录。
- 在桌面控制台分别验证只开 Web、只开游戏内显示、两者同开、两者全关四种矩阵。
- 只开游戏内显示时不得出现 FastAPI 端口、浏览器进程或 Web API 依赖。
- 关闭游戏内显示后不得残留 overlay、Vision sidecar、高频捕获或识别循环；低频监听状态必须可见、可关、可计量。
- 当前 MVP 只承诺 `Borderless` / 无边框全屏；`Full Screen` / 独占全屏不承诺独占全屏覆盖，后续只做检测与引导。
- 真实 LoL 人工验收需记录 P95 <= 500ms 的 overlay 文案更新延迟；识别输出目标 P95 <= 300ms。

下一截断的数据通道验收只覆盖本地 JSON 事件：

- `data/runtime/state/game_overlay_slots.v1.json` 是 overlay host 读取的本地三槽位事件文件。
- `python -c "from hextech.overlay.events import write_sample_overlay_event; print(write_sample_overlay_event())"` 可写入开发样例事件。
- `python -c "from hextech.overlay.events import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"` 可写入假识别事件，用于先验证“事件文件 -> overlay 三槽位渲染”的端到端通道。
- `python -m hextech.overlay.vision.sidecar --once --preset auto --write-event` 可执行一次本地 Vision 诊断探针；无 LoL 窗口时会写入 inactive 诊断事件。
- `python -m hextech.overlay.vision.sidecar --loop --preset auto --write-event` 是正式常驻链路；游戏窗口不存在或不在前台时低频待机，并只写一次 inactive 清理旧 active 事件；前台时按约 250ms 截图识别。
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
- `GET /api/live_state`：当前 LCU 英雄选择状态
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
- `python -m hextech.overlay --self-check`：独立模块只读自检
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
- 打包链路变更时同步检查 `tools/build_bundle.py`、`tools/bundle_manifest.py`、`tools/runtime_bundle.py`、`tools/dev_checks.py` 和本文档
