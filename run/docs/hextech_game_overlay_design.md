# Hextech 游戏内 Overlay 设计方案

本文是 `run/` 工作区内 Hextech 游戏内 overlay 的当前设计口径。它记录已经收敛的 MVP 行为、模块合同、显示语义、性能预算、验收方式和剩余风险；不替代 `PROJECT.md` / `README.md`，但应与两者保持一致。

当前工作面（双轨并行，本文档在所有分支保持同步）：

| 轨道 | worktree | 分支 | 数据源 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| 共享基座 | 同时存在于两个 worktree | 两条分支共有 | —— | 事件通道 + overlay host 已收敛，两轨复用，边界固定 |
| Track A 视觉识别 | `C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay` | `codex/feature/hextech-game-overlay` | 截图 + 按钮定位 + ROI 图标轮廓 + 卡名文字双通道 | **现行主线**：识别已重做并真机验证（2026-06-13，2/3 真机槽位认对，名字正确），剩余做稳（延迟/去抖/阈值/新卡覆盖）。详见 §0 与 §7.1 |
| Track B Overwolf GEP | `C:\Users\apple\worktrees\hextech-overlay-overwolf-gep` | `feature/hextech-overlay-overwolf-gep` | Overwolf GEP `augments` feature | **暂阻**：GEP 包对未注册 dev 应用只给空壳（v0.0.0、game-detected 0 次、RPC 全超时），需 Overwolf 开发者注册/审核才能通；代码就绪，等注册。执行计划见 `run/hextech_overwolf_gep_plan.md` |

- 目标工作区：`run/`
- 设计来源：`C:\Users\apple\.claude\plans\run-flickering-flame.md`、`run/README.md`、`run/PROJECT.md`、当前代码合同
- 同步要求：本设计文档是两轨共同的口径源，必须在 `main`、`codex/feature/hextech-game-overlay`、`feature/hextech-overlay-overwolf-gep` 三个分支上保持一致；改动任一处后同步其余分支。

## 0. 最终目标、数据架构与路线图（2026-06-13 固化，明天接续用）

### 0.1 最终产品目标
游戏内三选一界面出现时，overlay **在玩家选择之前就稳定显示**三张卡，每张卡展示：**卡名 + 品质(tier) + 中文介绍 + 胜率 + 出场率 + 英雄联动**。即从"认出名字"升级为"出装顾问"。当前只做到认名字，富内容是后续阶段。

### 0.2 数据架构（来源分工，已定）
| 内容 | 来源 | 说明 |
| :--- | :--- | :--- |
| 名字 + 品质 + 图标 | **官方 CommunityDragon**（`cherry-augments.json`，637 条，随补丁更新） | 同步工具 `tools/sync_cdragon_augments.py`；闭集用于视觉识别 |
| 中文介绍/描述 | **第三方**（aramgg `aram-mayhem-augments`） | 官方 arena 端点(`cdragon/arena/zh_cn.json`)有中文 desc 但仅覆盖 ~44%(228/516)，ARAM 卡如钢化你心/缩小引擎不在内，故描述沿用第三方 |
| 胜率 + 出场率 | **第三方**（aramgg / apexlol） | 私用开启（见 §12 更新口径） |
| 英雄联动(synergy) | **第三方**（已有 `data/raw/synergy/Champion_Synergy_*.json`） | 英雄×海克斯评分/强力联动/说明，待接入 overlay 渲染 |

dtodo 源已失效/剔除；第三方现存 aramgg、apexlol。

### 0.3 路线图（阶段）
- **阶段 1（现行，先做）— 识别做稳**：让 active 在选择期间稳定显示（延迟/去抖/门控）、slot 阈值余量重标、新卡覆盖。详细计划见 `C:\Users\apple\.claude\plans\run-overlay-giggly-iverson.md`。
- **阶段 2 — 富内容 overlay**：联动(synergy，仓里已有数据)优先接入渲染 → 再加胜率/出场率；overlay 渲染从"名字/品质/一句话"升级为多字段卡片。
- **阶段 3 — Track B Overwolf**：待用户完成 Overwolf 开发者注册放行后解阻，提供独占全屏渲染。

### 0.4 今日真机验收结论
新双通道识别（图标轮廓 + 卡名文字）真机三选一实测：active 触发、3 槽中 2 槽认对（最万用的瞄准镜、钢化你心，名字正确）。问题：① 显示太晚（选完才稳，需做稳延迟/去抖）；② 第 3 槽置信度贴近 0.80 门槛未 ready；③ overlay 内容仅名字、无胜率/出场率/联动（阶段 2 做）。

## 1. 当前目标

Hextech 伴生系统保留两个可独立控制的运行能力：

| 开关 | 当前目标 | 关闭时要求 |
| :--- | :--- | :--- |
| Web 前端 | 启动本地 FastAPI / Web 页面，用于浏览器详情页、英雄查询、数据调试和手动查看 | 停止 Web 进程、浏览器联动、WebSocket 和预热任务 |
| 游戏内显示 | LoL 游戏前台出现 active 选择事件时，把三槽位提示显示在游戏内海克斯卡片附近，目标 P95 文案更新延迟不高于 500ms | 停止 overlay host、Vision sidecar、高频截图和识别循环 |

两个开关互不强依赖：

- 只开 Web 前端：保留浏览器和 API 使用体验，不启动游戏 overlay 和截图识别。
- 只开游戏内显示：不强制启动 Web/FastAPI/浏览器，直接读取本地轻量 hint cache 和本地事件文件。
- 两者同开：Web 前端提供调试与详情页能力，游戏内 overlay 仍走同一份本地 cache / event contract。
- 两者全关：主 Tk 控制台保持轻量常驻，不进行高频截图、识别或浏览器协同。

显示模式按轨道分级：**Track A（Tk topmost）只承诺 LoL `Borderless` / 无边框全屏**，独占全屏盖不住，不通过注入/读内存绕过。**Track B（Overwolf overlay）通过官方图形钩子注入游戏渲染管线，可在 `Full Screen` / 真独占全屏下显示**，这是选择渲染接入的核心动机——一并解决数据准确与全屏覆盖。Track B 的渲染走 Overwolf 官方授权平台，不属于自研注入/读内存范畴。

## 1.5 技术路径分轨（双轨）

三槽候选数据的来源拆成两条可独立开发、独立开关的技术路径，二者共用同一套下游基座；这样视觉算法的重做与 Overwolf 接入互不阻塞。

### 共享基座

真正两轨共用的只有数据契约与映射，**渲染端按轨道分离**（这是渲染接入决策的直接结果）：

- `hextech/overlay/events.py`：三槽位事件协议（`schema_version`、`source`、`active`、`selection_type`、`slots[]`），唯一读写 `data/runtime/state/game_overlay_slots.v1.json`。Track A 必走它；Track B 可选择把候选镜像写入该文件作为调试 / Borderless fallback 契约，但 Track B 的全屏显示不依赖它。
- `data/static/version/`（及 `hextech/overlay/hints.py` 生成的提示缓存）：augment id → 名称 / tier / 描述的本地 JSON 映射。两轨共用同一份 JSON；Track A 用 Python 读，Track B 在 ow-electron 里直接读同一份 JSON。
- 渲染端**不共享**：Track A = `hextech/overlay/host.py` + `hextech/overlay/renderer.py`（Tk topmost，Borderless）；Track B = ow-electron Overwolf overlay（图形钩子，支持独占全屏）。Tk host 不参与 Track B 的全屏渲染。

### Track A — 视觉识别（截图链路）

- 工作树 / 分支：`...claudecode-codex-feature-hextech-game-overlay` / `codex/feature/hextech-game-overlay`。
- 范围：`hextech/overlay/vision/sidecar.py` 的窗口截图、蓝色按钮 anchor 定位、固定 ROI 切片、Pillow 指纹匹配。
- 当前状态：**冻结**。anchor 缓存中毒已修、`scene_active` 门控收紧（≥1 张 ready 才显示）、`--loop` 自动转储已加；但真机实测证明 24×24 灰度 NCC 指纹对细线条图标系统性认错（正确卡不进 top3，候选置信度全挤在 ~0.70 无区分度，见 §7.1）。图标匹配算法的重做留待后续单独立项。
- 角色：按 §7.1-3，作为 fallback / 诊断路径保留，不作为唯一长期主数据源。

### Track B — Overwolf GEP + Overlay 渲染（官方事件平台，目标全屏）

- 工作树 / 分支：`...hextech-overlay-overwolf-gep` / `feature/hextech-overlay-overwolf-gep`。
- 范围：新增一个 ow-electron 应用，**单进程同时干两件事**——订阅 Overwolf GEP `augments` feature 拿三张候选；用 Overwolf overlay 在游戏内渲染三卡。augment id → 中文名 / tier / 描述直接读 `data/static/version` 同一份 JSON。
- 关键能力：Overwolf overlay 注入游戏渲染管线，可在 **独占全屏（Full Screen）** 显示，突破 Track A（Tk topmost）只能 Borderless 的限制。
- 渲染归属：Track B 的三卡 UI 由 ow-electron overlay 渲染，**不走 Python Tk**。`hextech.overlay.host` 退居二线（Track A 渲染端 / Borderless fallback / 设置控制台）。事件通道可保留为调试镜像，非必需。
- 当前状态：**计划阶段**。执行步骤见 `run/hextech_overwolf_gep_plan.md`（仅存于本分支，不同步其他分支；交 Codex 执行）。依赖 / 包体 / 分发 / 渲染端迁移 go/no-go 见 §7.2。
- 角色：官方本地接口（Live Client Data / LCU）实测无法提供"未选三张候选"（NO-GO，见 §7.1）后，Track B 是目标主数据源 + 全屏渲染端。

## 2. 当前实现路线

当前 MVP 已收敛为：

- Tk 控制台管理用户开关与状态。
- `hextech/display/desktop/service_manager.py` 管理 Web 前端、overlay host、Vision sidecar 和低频监听生命周期。
- `hextech/overlay/host.py` 启动独立 Tk overlay host，使用 Win32 topmost / click-through / noactivate 样式覆盖 Borderless 游戏画面。
- `hextech/overlay/events.py` 作为本地三槽位事件协议，只读写 `data/runtime/state/game_overlay_slots.v1.json`。
- `hextech/overlay/vision/sidecar.py` 作为本地 Vision sidecar，使用 Pillow/pywin32 截取 LoL 游戏窗口，先用蓝色选择按钮做场景门控，再对固定三槽位 ROI 做模板指纹匹配并写入事件文件。
- `hextech/overlay/hints.py` 生成 overlay 本地轻量提示缓存，避免游戏内显示依赖 Web API。
- `tools/acceptance/overlay_performance_probe.py` 记录阶段 5 人工延迟样本摘要。

本节描述 Track A（视觉识别）的实现路线，是历史 MVP 主线。Track A 默认实现不引入 PySide/Qt、Electron、WebView2、OpenCV、imagehash、WGC 或 Overwolf。真实 LoL 验收已证明 Pillow/pywin32 + 固定 ROI 的指纹匹配不足（见 §7.1 视觉结论），因此并行开辟 Track B（Overwolf GEP，见 §1.5 / §7.2）；Track B 的 ow-electron / Overwolf 依赖只属于 Track B 工作树，不回灌 Track A 的依赖约束。

## 3. 四问题修复口径

本设计按最新四问题修复计划锁定以下结论：

1. 位置不居中：游戏内水平居中实际生效；用户看到的不居中来自客户端/桌面阶段找不到 `League of Legends (TM) Client` 游戏窗口，overlay 停在初始 `+240+132` 且 topmost 浮在前台窗口上。
2. `Alt+H` 无效：旧实现把 `RegisterHotKey` 绑到 Tk hwnd，WM_HOTKEY 被 Tk 消息泵吃掉；现在改为独立 daemon 线程 `RegisterHotKey(NULL, ...)` + 阻塞 `GetMessageW`，通过 `queue.Queue` 通知 Tk 主线程翻转用户开关。
3. 占位框常驻：旧实现算出 `event_visible` 但显示条件只看 `user_enabled`；现在改为用户开关、active 事件、游戏窗口前台三与门。
4. 正式选择无数据：旧 sidecar 只有 `--once` 单次探针，ServiceManager 在客户端阶段启动后立即退出；现在保留 `--once` 诊断，正式链路使用 `--loop` 常驻自门控循环。

附加修复：Vision sidecar 进程启动时设置 DPI awareness，避免 Windows 缩放下窗口坐标虚拟化导致截图 ROI 偏移。

## 4. 显示语义

Overlay 默认不显示占位框。窗口显示必须同时满足：

```text
user_enabled
AND active 选择事件
AND LoL 游戏窗口在前台
```

具体规则：

- 无事件文件、事件损坏、事件过期、inactive 事件、客户端阶段、桌面前台、游戏切后台时，overlay 全部隐藏。
- `active` 可见事件必须来自本地事件文件，且 `selection_type` 为 `hextech`；`body_shard` 只保留为诊断选择态，不触发 overlay 显示。
- 蓝色选择按钮是游戏内显示的主场景门控：按钮不存在时，sidecar 不检测三张卡，overlay 保持隐藏。
- 锻体碎片三选一必须隐藏 overlay，并写出 `body_shard_only` 诊断原因。
- `Alt+H` 只切换 `user_enabled`，不绕过事件门控和前台门控。
- overlay host 启动时先 `withdraw()`，避免在桌面或客户端阶段露出初始位置。
- 只有显隐状态翻转时才调用 deiconify/withdraw 和 Win32 样式应用，避免高频重复 SetWindowPos。
- 调试样例事件也必须配合游戏窗口前台才会显示；这保证调试链路不会破坏正式显隐语义。

## 5. 模块合同

| 模块 | 职责 | 明确不负责 |
| :--- | :--- | :--- |
| `hextech/display/desktop/app.py` | 桌面控制台 UI、开关交互、状态展示 | 不堆积子进程生命周期细节 |
| `hextech/display/desktop/service_manager.py` | 管理 Web 生命周期，并把“游戏内显示”开关委托给统一 Controller | 不直接启动或分别停止 host/sidecar |
| `hextech/overlay/lifecycle.py` | host + Vision sidecar 原子启停、失败回滚、统一状态快照 | 不绘制、不解释 Web 生命周期 |
| `hextech/overlay/host.py` | 透明置顶窗口、点击穿透、窗口跟随、`Alt+H`、显隐门和最小事件轮询 | 不截图、不识别、不访问 Web API |
| `hextech/overlay/data_source.py` | `OverlayDataSource` 协议和当前 shared hextech adapter | 不向 renderer 暴露 Web 模型 |
| `hextech/overlay/renderer.py` | 三个统计窗、0–3 条真实命中联动和 Canvas 绘制 | 不读文件、不启动进程、不绘制诊断状态 UI |
| `hextech/overlay/events.py` | 本地 JSON 事件协议、规范化、过期/损坏/缺失诊断、样例/假识别/inactive 事件写入 | 不做截图识别、不抓远端、不自动点击 |
| `hextech/overlay/vision/sidecar.py` | `--once` 诊断探针、`--loop` 常驻识别、自门控待机、DPI awareness、事件写入 | 不读游戏内存、不注入、不修改客户端、不依赖 Web 服务 |
| `hextech/overlay/hints.py` | overlay 轻量提示缓存生成与按 augment_id 查询 | 不触发远端抓取阻塞游戏内显示 |
| `tests/` | 唯一自动化测试事实源；承载 overlay 合同回归及 marker 门禁 | 不替代真实 LoL 人工验收 |
| `tools/dev_checks.py` | 兼容 CLI；按 marker 委托 pytest，并保留 bundle manifest、健康摘要和 Web 联动人工辅助模式 | 不保存自动化断言，不替代真实 LoL 人工验收 |
| `tools/acceptance/overlay_performance_probe.py` | 手工延迟样本 P50/P95 摘要 | 不自动测游戏画面端到端延迟 |

## 6. 数据流

正式游戏内显示链路：

```text
Tk 控制台游戏内显示开关开启
  -> ServiceManager 调用 GameOverlayController.start()
  -> Controller 准备 shared hint cache、写 inactive、启动 sidecar 与 host 并等待 readiness
  -> sidecar 未找到游戏窗口或游戏不在前台：低频待机，不截图
  -> sidecar 找到前台游戏窗口：无校准缓存时先全屏定位一次蓝色选择按钮
  -> sidecar 有校准缓存后：每轮只检测固定按钮 ROI
  -> 按钮不存在：写 inactive 诊断事件，不进入三卡识别
  -> 按钮存在：约 250ms 一轮检测三张卡片 ROI
  -> sidecar 稳定识别 active 三槽位：写入 game_overlay_slots.v1.json
  -> overlay host 轮询事件文件并重绘三槽位
  -> 三与门通过时显示，否则隐藏
```

本地事件文件：

```text
data/runtime/state/game_overlay_slots.v1.json
```

按钮校准缓存：

```text
data/runtime/state/overlay_anchor_calibration.v1.json
```

该缓存只保存当前环境的按钮 ROI、三槽位 ROI、窗口尺寸和校准版本。它属于运行态文件，不是发布源数据。

事件通道要求：

- 只接受本地 JSON 文件，不通过 WebSocket/HTTP 作为当前 MVP 必需运行时。
- 缺失、损坏、schema 不匹配、过期必须可诊断。
- active 期间 sidecar 通过心跳重写防止 5 分钟过期。
- 槽位签名变化时才写事件，减少无意义 IO。
- 从 active 变为不稳定、卡片消失或游戏切后台时写 inactive，避免旧提示残留。

## 7. Vision Sidecar

`hextech/overlay/vision/sidecar.py` 提供两个入口：

```powershell
# 诊断探针：执行一次短窗口识别后退出
python -m hextech.overlay.vision.sidecar --once --preset auto --write-event

# 正式链路：常驻自门控识别循环
python -m hextech.overlay.vision.sidecar --loop --preset auto --write-event
```

`--loop` 行为：

- 游戏窗口不存在或不是前台：低频待机，默认不截图。
- 游戏前台且无 `overlay_anchor_calibration.v1.json`：在宽搜索区内扫描一次底部蓝色选择按钮，定位成功后写入校准缓存。
- 游戏前台且已有有效校准缓存：每轮只 crop 固定按钮 ROI；按钮不存在时写 `selection_button_missing` 并隐藏 overlay。
- 按钮存在：按约 250ms frame interval 检测三槽位 ROI。
- 按钮首次定位失败：写 `anchor_missing`，不检测三张卡，等待后续帧重新尝试。
- 三张卡判定为锻体碎片：写 `body_shard_only`，不显示 overlay。
- 稳定策略：沿用 required frames，默认 2 帧稳定后写 active。
- 写入策略：槽位签名变化或心跳到期时写事件。
- 模板缺失：写 `template_missing` 诊断并退出，避免空转。
- DPI：进程启动时设置 DPI awareness，减少缩放下 ROI 偏移。

当前 ROI 预设覆盖：

- `1920x1080`
- `2560x1440`
- `2560x1600`
- `auto`

真实 LoL 下 ROI、置信度阈值和多显示器/DPI 行为仍需人工验收，不在文档里盲调。

### 7.1 官方接口优先验证顺序

后续真实 LoL 验收完成后，三槽候选数据源的升级顺序固定为：

1. 先验证 Riot / LoL 官方本地接口是否能走通三槽候选数据。
2. 官方接口拿不到稳定三槽候选时，再进入部分接入 Overwolf GEP 的备用路线。
3. Vision sidecar 保留为 fallback / 诊断路径，不再作为唯一长期主数据源假设。

官方接口验证范围：

- LCU lockfile 连接：只用于本地 League Client API，验证 gameflow、champ-select、summoner 和可能存在的 Arena / augment 相关 endpoint。
- Live Client Data：只访问 LoL 官方本地 live client interface，验证是否能在局内读到当前玩家状态、已选 augment、候选 augment 或足够可靠的选择态信号。
- CommunityDragon / 本地静态数据：只作为 augment id、名称、图标、描述映射，不当作“当前三槽候选”的实时来源。

判定标准：

- 若官方接口能稳定给出当前三张候选 augment id / name，则新增官方接口 provider，并写入现有 `game_overlay_slots.v1.json` 事件协议。
- 若官方接口只能给出 gameflow、已选 augment 或选择态信号，不能给出三张未选择候选，则继续保留它作为场景门控 / 诊断辅助，不替代三槽 provider。
- 若官方接口没有可用候选数据，再进入 `7.2` 的 Overwolf GEP provider 验证。

#### 真机验证结论（2026-06-12，已实测，NO-GO）

| 来源 | 实测结果 | 结论 |
| :--- | :--- | :--- |
| Live Client Data（`https://127.0.0.1:2999`） | 局内 36 份 `allgamedata` 采样，4 个 endpoint 全 200；顶层只有 `activePlayer / allPlayers / gameData / events`，递归扫描 **augment/hextech 字段命中 = 0**（`activePlayer` 仅金币/符文/技能/属性，`events` 仅 GameStart/MinionsSpawning） | 官方局内接口**不暴露**未选三张候选 |
| LCU（LeagueClientUx 进程参数取端口/token） | 连通，仅大厅类数据（gameflow/champ-select/summoner），无候选字段 | 只能作门控/诊断 |

判定：官方本地接口（Live Client Data + LCU）**无法作为三槽候选数据源**，按上面第 3 条进入 §7.2 Overwolf GEP。官方接口后续仅可作为场景门控 / 诊断辅助。

#### 视觉识别真机结论（Track A，同日实测）

按钮 anchor 定位与 ROI 切片在真机正确（slot crop 精准命中图标），模板库 208 条也含真实卡（如「缩小引擎」「坦克引擎」「尤里卡」均在库）。但 24×24 灰度 NCC 指纹匹配**系统性认错**：真卡「尤里卡」识别 top3 为中娅/循环往复/狂妄，三候选置信度全挤在 ~0.70（margin≈0，无区分度）。根因是"大片黑底 + 细线条小图标"降采样后细节丢失。结论：降阈值无意义，需重做匹配算法（更紧的图标裁剪 / 更高分辨率指纹 / 边缘描述子），单独立项，Track A 暂冻结。

### 7.2 ARAMBro / Overwolf GEP 路线（Track B，现行优先数据源）

官方本地接口 NO-GO 后，本节从"备用登记"升级为 **Track B 现行优先数据源**。具体执行步骤见 `run/hextech_overwolf_gep_plan.md`（**仅存于 `feature/hextech-overlay-overwolf-gep` 分支，不随本设计文档同步到 main / 视觉分支**；交 Codex 执行）；采用前仍受本节末尾 go/no-go 约束。当前主线数据源策略：Track B 为目标主源，Track A 视觉作 fallback / 诊断。

只读检查 `C:\Users\apple\AppData\Local\Programs\ARAMBro\resources\app.asar` 后，ARAMBro 的关键做法可归纳为：

- 它把 Overwolf GEP 作为局内事件数据源，`LOL_GEP_REQUIRED_FEATURES` 包含 `augments`，并对 LoL / PBE game id 调用 `gep.setRequiredFeatures(...)`。
- 它监听 `new-info-update`、`new-game-event`、`game-detected` 等 GEP 事件，并每 1000ms 调用 `gep.getInfo(gameId)` 补抓当前快照。
- 它从 `owGep.stateManager.getState().augments` 读取当前候选，通过 `extractGepAugmentRefs(...)` 和 `buildGameStateSnapshotFromGepAugments(...)` 解析三张候选。
- 它的 Dev UI 暴露 `owgep:get-state`，并能从 `augments.augment_1` / `augment1`、`augment_2` / `augment2`、`augment_3` / `augment3` 读取测试槽位。
- LCU 更像是 gameflow、champ-select、summoner 等控制面来源；三张局内候选的主数据源是 GEP `augments`，评分和推荐再接本地 augment 统计数据。

采用的接入深度：**渲染接入**（用户已定）。一并解决两个问题——GEP 给准确候选，Overwolf overlay 给独占全屏覆盖；代价是渲染端从 Python Tk 迁到 ow-electron。架构边界：

```text
ow-electron 单进程
  ├─ 订阅 Overwolf GEP augments feature（augment_1/2/3）
  ├─ 读 data/static/version 同一份 JSON 把 id 补成中文名/tier/描述
  ├─ Overwolf overlay 窗口在游戏内渲染三卡（独占全屏可见）
  └─（可选）镜像写 game_overlay_slots.v1.json，供 Tk 在 Borderless 作 fallback / 调试
```

为什么不是"GEP 只采数据 + Python Tk 渲染"：那条（数据接入）能修数据，但 Tk topmost 盖不住独占全屏，全屏仍不可见。要全屏必须用 Overwolf overlay 的图形钩子渲染，故渲染端必须迁出 Python。

约束：

- 不导入、复用或修改 ARAMBro 安装目录代码；只把它作为已验证实现思路参考。
- Track A 的 `hextech.overlay.host` 不依赖 GEP；Track B 的 ow-electron 不反向依赖 Python overlay host。两端唯一可选的耦合是事件文件镜像。
- Vision sidecar 保留为 fallback / 诊断路径；ow-electron Track B 必须能独立开关，不破坏 Track A。
- 渲染接入的 go/no-go（比数据接入更重，采用前需用户逐项确认）：① ow-electron + Overwolf 运行时依赖（数百 MB，产物非纯便携）；② 整套三卡 UI 用 ow-electron overlay 重画（renderer 迁移）；③ Vanguard / Riot 对第三方 GEP overlay 应用的放行；④ 分发方式与一次性 Overwolf app 注册。GEP 接口与 ow-electron 包本身免费，成本在依赖与分发，不在费用。

## 8. Overlay Host

`hextech/overlay/host.py` 的当前设计边界：

- 根入口：`python hextech_ui.py --game-overlay` 或 `python -m hextech.overlay.host`。
- 启动后默认隐藏，等待三与门放行。
- 跟随窗口标题：以 LoL 游戏窗口为目标，读取 hwnd/rect 并计算 overlay 几何。
- 显示位置：游戏内顶部水平居中，距顶约 132px，维持当前 MVP 口径。
- 点击穿透：默认不阻挡玩家选择。
- noactivate：overlay 自身不抢前台，前台判断只看 LoL 游戏 hwnd。
- 热键：`Alt+H` 全局热键线程投递 toggle 请求，Tk 渲染轮询中消费。
- 渲染：只根据 `read_overlay_event()` 的 snapshot 绘制三槽位，不触发识别或远端访问。

## 9. Web 前端解耦

游戏内显示单开时不得依赖：

- FastAPI 端口。
- 浏览器进程。
- WebSocket。
- `/api/live_state` 或其他 Web API。
- 远端网页抓取。

Web 前端仍可作为调试和详情页入口，但不是 overlay 热路径。overlay 使用本地 hint cache 与本地事件文件完成渲染。

## 10. 性能预算

硬目标：从海克斯卡片开始出现起，overlay 文案更新 P95 <= 500ms。

当前内部目标：

| 阶段 | 目标 | 说明 |
| :--- | :--- | :--- |
| 前台检测 | 低频/即时切换 | 游戏非前台时不截图 |
| ROI 捕获 | 16-50ms | 只截游戏窗口固定区域 |
| 图标识别 | 5-40ms | 当前用本地模板指纹匹配 |
| 稳定去抖 | 2 帧左右 | 防止动画和单帧误识别 |
| 本地提示查询 | 1-5ms | 读本地 hint cache |
| 事件写入/轮询 | 约 250ms 节拍 | 当前以简单 JSON 文件换取低耦合 |
| Overlay 渲染 | 16-50ms | 三槽位文本和标签 |

验收指标：

- 识别输出 P95 <= 300ms。
- overlay 文案更新 P95 <= 500ms。
- 关闭游戏内显示后无 overlay、Vision sidecar、高频捕获或识别循环残留。
- 低频监听必须可见、可关、可计量。

## 11. 验收方式

自动化测试以 pytest 为唯一事实源；旧命令作为兼容入口委托
`pytest -m "dev_gate and not deep"`：

```powershell
cd C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay\run
python tools/dev_checks.py

# overlay fast：pytest -m "dev_gate and overlay and not deep"
python tools/dev_checks.py --overlay-only

# overlay 深度门禁：pytest -m "dev_gate and overlay"
python tools/dev_checks.py --overlay-only --deep
```

人工辅助命令：

```powershell
# overlay host；无 active 事件或游戏不在前台时应保持隐藏
python hextech_ui.py --game-overlay

# 写入样例事件；仍需 LoL 游戏窗口前台才会显示
python -c "from hextech.overlay.events import write_sample_overlay_event; print(write_sample_overlay_event())"

# 写入假识别事件，验证事件文件到三槽位渲染
python -c "from hextech.overlay.events import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"

# 一次性 Vision 诊断探针
python -m hextech.overlay.vision.sidecar --once --preset auto --write-event

# 正式常驻链路
python -m hextech.overlay.vision.sidecar --loop --preset auto --write-event

# 官方接口优先验证；默认只读，不写 overlay event
python tools/acceptance/probe_official_overlay_provider.py --duration-seconds 120 --interval-ms 500 --dump-runtime-json

# 写入 inactive，overlay 应隐藏
python -c "from hextech.overlay.events import write_inactive_overlay_event; print(write_inactive_overlay_event())"

# 手工延迟样本摘要
python tools/acceptance/overlay_performance_probe.py --latency-ms 180 240 420 --source-tag manual-lol-borderless
```

真实 LoL 验收需要用户配合训练模式或实局：

- 客户端阶段 overlay 不可见。
- 无 active 事件时 overlay 不可见。
- 写入 active 样例事件但游戏不在前台时 overlay 不可见。
- LoL `Borderless` / 无边框全屏前台且 active 事件存在时 overlay 可见。
- 无关游戏页面或蓝色选择按钮不存在时 overlay 不可见。
- 锻体碎片三选一时 overlay 不可见，事件原因应为 `body_shard_only`。
- 海克斯卡片出现后约 1 秒内显示三槽位真实数据。
- 选择结束、事件 inactive 或事件过期后 overlay 自动隐藏。
- `Alt+H` 在游戏焦点下能切换用户开关，并且不绕过事件/前台门控。
- Alt+Tab 切出游戏后 overlay 跟着隐藏。
- 关闭游戏内显示后无 overlay host / Vision sidecar 残留。
- 记录识别输出 P95 <= 300ms、overlay 文案更新 P95 <= 500ms。

## 12. 合规与安全边界

必须遵守：

- 不读取或修改 token、cookie、API key、proxy secret、auth 文件。
- 不注入 LoL 进程。
- 不读取 LoL 进程内存。
- 不修改游戏客户端文件。
- 不自动点击、自动选择或自动输入。
- 不把运行态日志、截图缓存、profile、raw 数据作为发布源数据提交。
- 不把 `data/runtime/**` 作为 Git 提交内容。
- 不把 `data/runtime/state/overlay_anchor_calibration.v1.json` 打入 bundle manifest、PyInstaller `_internal`、源码数据或便携包；打包后首次启动必须重新校准。

胜率/排名（2026-06-13 用户决策更新）：

- **私用场景已批准开启**：用户明确要在本机私用 overlay 中显示胜率/出场率（阶段 2 实现）。
- 仍标注风险：**不宣称 Riot 合规**，不作为对外默认可发布能力；数据来自第三方(aramgg/apexlol)。
- 可发布/分发版本仍受限：默认发布构建不得把 Augments / Arena win rate 写成"合规" overlay 内容；私用开关与发布构建解耦。
- 历史口径 `policy_blocked` 仅对"对外发布"仍成立；"本机私用"已 go(此条即 §0.2 表中胜率/出场率"私用开启"的来源)。

## 13. 当前剩余风险

| 风险 | 影响 | 处理方式 |
| :--- | :--- | :--- |
| 真实 LoL Borderless 下 ROI 偏移 | 识别不到或 slot 串位 | 用 `--once --write-event` 收集输出后再调 ROI/阈值 |
| 真实截图置信度不足 | active 事件不稳定或错误 | 只基于真实探针数据调整模板/阈值，不盲调 |
| DPI / 多显示器差异 | 截图 bbox 与窗口位置不一致 | sidecar 已设 DPI awareness；剩余差异列入人工验收 |
| `Alt+H` 注册失败 | 无法热键切换 | 保留 warning 日志，需人工确认是否被系统/其他软件占用 |
| Borderless 覆盖不稳定 | overlay 不可见或跟随错误 | 当前 MVP 只承诺 Borderless，真实表现需训练模式验收 |
| Full Screen / 独占全屏 | 普通外部窗口无法覆盖 | 不作为当前 MVP；后续检测并引导切无边框 |
| 胜率/排名政策冲突 | 不能作为默认可发布能力 | 私用已 go（§12）；发布构建仍 `policy_blocked`，私用与发布解耦 |

## 14. 当前结论

游戏内 overlay 的当前 MVP 已从“未来方案”收敛到一条明确实现线：Tk overlay host 只负责窗口、热键、跟随和渲染；Vision sidecar 负责常驻自门控识别；ServiceManager 负责生命周期；本地 JSON 事件文件负责进程间契约；本地 hint cache 负责 Web 解耦。

最新显示语义是默认不显示占位框，只有“用户开关开启 + active 海克斯选择事件 + LoL 游戏窗口前台”同时满足才显示。蓝色选择按钮检测分为两步：新环境首次定位按钮并写运行态校准缓存，后续每轮仍用固定按钮 ROI 确认选择场景存在。剩余工作不是重新选技术栈，而是在真实 LoL `Borderless` / 无边框全屏下完成人工验收，并根据实际截图证据修正 ROI、阈值和性能预算。

## 15. 2026-06-15 晚间实测与 2026-06-16 接续方案

### 15.1 晚间实测现状

用户在真实游戏选择阶段已肉眼确认：悬浮窗能在游戏内出现，三张卡的识别内容与实际选择项对得上。实测捕获过的三槽示例为：

| 槽位 | 卡名 | 品质 | 当前本地 hint 状态 |
| :--- | :--- | :--- | :--- |
| 1 | 无限循环往复 | 棱彩 | 可查到胜率与出场率 |
| 2 | 你摸不到 | 棱彩 | 可查到胜率、出场率与 synergy |
| 3 | 尤里卡 | 棱彩 | 可查到胜率、出场率与 synergy |

晚间发现两个用户可见问题：

- 悬浮窗闪烁：选择页期间 Vision sidecar 会在 `selection_scene_not_detected`、`unstable`、`selection_button_missing` 与 active 事件之间短周期切换，导致 host 按 inactive 立即隐藏再显示。
- 胜率/出场率缺失：overlay hint cache 当时是空缓存；根因是正式预计算缓存相对最新 CSV 被判 stale，生成 overlay cache 时直接降级为 `cache_missing`。

### 15.2 晚间已处置状态

- 已给 host 增加短暂 active 保持：刚刚显示过 active 三槽后，遇到上述 transient inactive 原因时短暂保留上一帧，避免单帧/短帧抖动导致窗口闪烁。
- 已让 overlay hint cache 在正式预计算缓存 stale 时优先消费本地已有详情快照，避免选择阶段同步重建 170+ 英雄详情，也避免空 cache。
- 已重写运行态 `overlay_hint_cache.v1.json`：当前本地 cache 有 182 条 hint，私用统计开启。
- 已重启 overlay host，使防闪烁逻辑生效；Vision sidecar 和桌面 UI 保持运行。
- 当前本地英雄上下文已能由 LCU 写入；晚间 probe 见到的 context 示例是“不祥之刃”和“破败之王”，说明 context 链路已经活。

晚间验证命令：

```powershell
python -B -m py_compile hextech\overlay\hints.py hextech\overlay\host.py tools\dev_checks.py
python -B -m hextech.overlay.host --self-check
python -B tools\dev_checks.py
git diff --check
```

结果：上述验证通过；`python -m hextech.overlay.host --self-check` 显示 hint cache 无错误、context ok。

### 15.3 2026-06-16 第一优先级：真实选择页复验

目标：确认晚间修复是否解决用户肉眼问题，不再先调算法。

复验顺序：

1. 启动桌面 UI，确认“游戏内显示”和“私用统计”开启。
2. 进入 LoL `Borderless` / 无边框全屏，等待真实海克斯三选一。
3. 用户肉眼确认悬浮窗是否仍闪烁。
4. 用户肉眼确认每张卡第二行是否显示 `胜率 xx.x% · 出场 xx.x%`。
5. 若当前英雄已锁定，确认有 synergy 命中的卡是否显示英雄联动；无命中时应显示“暂无当前英雄联动”，不应乱配英雄。
6. 选择结束后确认 overlay 能自动隐藏，不能长时间残留上一组三卡。

通过标准：

- active 选择阶段悬浮窗稳定显示，不出现可感知闪烁。
- 三张卡卡名/tier 与游戏内选择项一致。
- 私用统计开启时显示胜率/出场率；关闭私用统计后不显示。
- 当前英雄 context 存在时，联动文案只按当前英雄匹配。
- 选择结束后 overlay 及时隐藏，允许短暂保持但不得残留超过约 1 秒。

### 15.4 若仍闪烁：明日定位方案

先判断是哪类闪烁，不直接改阈值：

| 现象 | 判断 | 下一步 |
| :--- | :--- | :--- |
| 窗口整块消失再出现 | event/visibility 抖动 | 记录 0.5s 轮询日志，查看 active/inactive 原因；只调整 hold 秒数或 transient reason 列表 |
| 窗口不消失但内容跳空 | slot ready 抖动 | 检查三槽 slot 状态是否在 ready/low_confidence/detecting 间切换 |
| 只在选完后残留 | hold 过长或 inactive 边沿没写 | 缩短 hold 或保证选择结束事件覆盖上一帧 |
| 游戏切后台仍显示 | 前台门控问题 | 复查 game foreground 判断，不扩大显示条件 |

建议先用如下只读监听，不读取 token，不改运行态：

```powershell
@'
import time
from hextech.overlay.events import read_overlay_event
for i in range(80):
    event = read_overlay_event()
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    print(i, event.get("visible"), event.get("active"), source.get("reason"), [
        (slot.get("state"), slot.get("name"), slot.get("tier")) for slot in event.get("slots", [])
    ])
    time.sleep(0.5)
'@ | python -B -
```

### 15.5 若仍无胜率/出场率：明日定位方案

优先从功能链路判断：

1. 确认私用统计开关是否开启。
2. 确认 `overlay_hint_cache.v1.json` 是否非空、`private_policy_stats_enabled=True`。
3. 用卡名查询 hint，确认 `winrate/pickrate` 字段存在。
4. 如果 cache 有字段但画面不显示，再查 host 渲染；如果 cache 无字段，则查本地详情快照和最新 CSV freshness。

可用只读检查：

```powershell
@'
from hextech.overlay.hints import load_overlay_hint_cache, query_overlay_hint
cache = load_overlay_hint_cache()
print("error=", cache.get("error"), "hints=", len(cache.get("hints") or {}), "private=", (cache.get("source") or {}).get("private_policy_stats_enabled"))
for name in ["无限循环往复", "你摸不到", "尤里卡"]:
    result = query_overlay_hint(cache, name)
    hint = result.get("hint") if result.get("ok") else {}
    print(name, result.get("ok"), hint.get("winrate"), hint.get("pickrate"), len(hint.get("synergies") or []))
'@ | python -B -
```

### 15.6 明日不要做的事

- 不在没有新证据时继续调视觉阈值。
- 不改事件协议 schema。
- 不把 `data/runtime/**`、`overlay_anchor_calibration.v1.json` 或运行态 cache 提交。
- 不触碰 cdragon sync、assets、`Augment_Icon_Manifest.json`。
- 不读取或修改 token、cookie、auth、API key、proxy 配置。
- 不把私用胜率/出场率包装成对外发布合规能力。

### 15.7 明日收口条件

若真实复验通过：

- 保留当前 host 防闪烁与 stale 本地详情快照降级方案。
- 补充最终验收记录：三选一截图/肉眼结果、三槽卡名、是否显示 stats、是否显示英雄联动、选择后隐藏是否正常。
- 运行 `python -B tools\dev_checks.py` 和 `git diff --check`。
- 只在用户明确要求时 staging/commit/push。

若真实复验仍失败：

- 先保存 0.5s 事件轮询摘要和当前三张卡名。
- 按 §15.4 或 §15.5 分类处理，不扩大到 Track B、Overwolf、cdragon 或资源同步。

### 15.6 2026-06-19 前端显示修复 + UI 视觉重塑（Hextech 原生风格）

后端 `1c3852b` / `26f7824` 把 `event.source.*` 与 `slot.*` schema 大改后，游戏内 Tk
overlay（`run/hextech/overlay/host.py`）几乎没消费新字段；同时 UI 风格被反馈"不够
高级"。本轮按计划 `~/.claude/plans/overlay-steady-pie.md` 修复，工作范围严格限定为游戏内
overlay + dev_checks，Web 前端静态产物位于 `run/hextech/display/web/static/`。

**显隐决策表（plan §一）改动**：`_should_show_overlay` 不再要求 `content_ready==True`。
`selection_window_active==True` 一律允许显示，渲染层按 `slot.state` 决定画完整卡 / 骨架卡 /
等待卡。`event.error` 非空与 `blocking_modal=True`（sidecar 已写 `selection_window_active=False`）
正常模式仍隐藏，仅 `config.diagnostic_mode=True` 时显示窄状态条。旧事件
（`selection_window_active is None`）保持向后兼容回退到 `event_visible+content_ready`。

**数据层（plan §三）改动**：`build_overlay_render_rows` 扩字段
`state` / `summary` / `synergy_rows`（结构化保留 `hero_name`/`rating`/`tag`/`tier`/`content`）/
`confidence` / `confidence_label` / `diagnostic_zh` / `top_candidates` / `tags`（**仅 hint.tags，
不并入 `source_heroes`**）/ `condition`；旧字段 `ready` / `title` / `stats` / `synergy` /
`synergy_state` 完全保留向后兼容。`_format_private_stats` 改三态：策略关 → "已开启隐私模式"
muted；策略开但当前英雄缺数据 → "暂无该英雄统计" muted；有数据 → "胜率 X · 出场 Y" good。`DIAGNOSTIC_ZH`
覆盖 `overlay_vision_sidecar.py` 当前所有 `diagnostic=` 真实码（dev_checks 通过 grep 校验
覆盖率），未知码原样返回。新增 `_extract_event_status(snapshot)` 集中读 `event.source.*`，
旧事件优雅 fallback。

**视觉层（plan §二）改动**：在文件顶部加 `OVERLAY_THEME` token dict 集中管理颜色。
`_draw_hextech_panel` 升级为 4 层视觉：tier 描边（Prismatic/Gold/Silver 各色）+ 暗色内描边 +
顶部高光线 + 左侧发光柱。新增 `_draw_status_banner`（顶部 selection_label + gate_state 中文徽章 +
状态 dot），`_draw_skeleton_panel`（partial_ready / detecting 占位卡），`_draw_card_chrome`
（ready 卡内：tier chip + title + 置信度圆点 + stats + summary + 底部置信度进度条 + 诊断码），
`_draw_low_confidence_candidates`（low_confidence 槽切换详情区为 top_candidates 列表）。
`_draw_overlay_detail` 改为消费结构化 `synergy_rows`（rating/tag/hero_name/content 分行）。
`_resolve_overlay_layout` 加 `banner` box，right 模式 `badge_y0` 让出 24px、top 模式 banner
复用 `detail_box` 顶部，三档 viewport（1366×768 / 1920×1080 / 2560×1600）几何不重叠。
`blocking_modal` / `event.error` 状态条用 `stipple="gray50"` 模拟半透明（Tk Canvas 不接受
8 位 RGBA），不再尝试全屏遮罩。

**banner 实时性（plan §三·3.8 / reviewer 阻断 4）**：`_resolve_stable_render_snapshot`
仍单返回保持兼容，但渲染循环 `_schedule_event_render` 把"最新事件"额外写到
`visibility["live_status_snapshot"]`，`_draw_overlay_snapshot` 接 `live_status_snapshot=`
入参专门驱动 banner / 离线条 / 诊断码——避免 partial_ready / unstable 期间 banner 显示
hold 后的旧 gate_state。

**augment 图标本轮不上**：`hint.icon` 是远端 URL，本地缓存机制需新建。本轮在槽位左上角
用 tier 色 chip 占位，给 `_draw_card_chrome` 留接口。下一轮再接图标缓存。

**dev_checks 离线契约（plan §五·5.1）**：`run_overlay_only_checks` 新增 9 个独立 check：
`check_overlay_diagnostic_translation_table` /
`check_overlay_format_private_stats_three_states` /
`check_overlay_extract_event_status_legacy` /
`check_overlay_render_rows_low_confidence_and_top_candidates` /
`check_overlay_render_rows_excludes_source_heroes_from_tags` /
`check_overlay_visibility_decision_table` /
`check_overlay_stable_snapshot_separates_live_status` /
`check_overlay_layout_three_viewports` /
`check_overlay_draw_perf_smoke`。同步更新现有 `check_game_overlay_host_contract`
里的决策表与 row schema 断言。

**离线渲染快照工具（plan §五·5.2）**：新增 `run/tools/overlay_render_snapshot.py`，
内置 6 个 fixture（`ready_three_tiers` / `partial_ready` / `event_error` / `blocking_modal` /
`privacy_off` / `stats_missing`），通过 `python -m tools.overlay_render_snapshot --case all`
导出到 `run/data/runtime/debug/overlay_snapshot_<case>.{ps,png}` 供肉眼对比。无 Ghostscript 时
回退到 `.ps`，开发可用 PS Viewer 直接打开。

**性能桩（plan §五·5.4）**：`_draw_overlay_snapshot` 入口加 `time.perf_counter()`
桩，把 ``last_draw_ms`` 写到 `perf_sink`（渲染循环里指向 `visibility`）；`check_overlay_draw_perf_smoke`
跑 50 次合成 fixture 验证桩存在且耗时合理。下一轮在真机加严绘制 P95 阈值。

**Git 边界**：本轮未 commit、未 push、未合并；改动只叠加在 `run/hextech/overlay/host.py`
+ `run/tools/dev_checks.py` + 新增 `run/tools/overlay_render_snapshot.py` + 本节文档。
worktree 既有 13 个修改文件 + 2 个未跟踪文件保持不变。未做真机视觉验收；离线 dev_checks
`--overlay-only` exit 0、6 张快照写出。

## 16. Living Design 接续账本（overlay worktree 为主）

本节是后续多轮对话的短接续入口，优先服务 `C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay`。主仓 `main` 只作为后续同步目标；当前实现判断、试错沉淀和人工复验均以本 worktree 为主。

### 16.1 最终目标

- 游戏内三选一出现时，在玩家选择前稳定显示三张卡。
- 每张卡展示卡名、品质、中文机制说明、胜率、出场率和当前英雄联动。
- Track A 先保证 LoL `Borderless` / 无边框全屏下可用；Track B 等 Overwolf / GEP 外部条件满足后再接独占全屏。
- 当前阶段不追求自动选择、不读内存、不注入、不修改游戏客户端。

### 16.2 当前状态

- 已实现：Tk overlay host、三槽事件文件、Vision sidecar、hint cache、私用统计字段、英雄 context 渲染链路。
- 可用但不稳定：真实选择页显示稳定性、识别稳定性、选完隐藏和富内容实际画面显示仍需复验。
- 当前 runtime 快照（2026-06-16 fresh probe）：`game_overlay_slots.v1.json` 为 `manual-hide`、非 active；`python -m hextech.overlay.host --self-check` 显示 `event_expired`、`context_expired`。
- 当前内容缓存：`overlay_hint_cache.v1.json` 存在，182 条 hint，`private_policy_stats_enabled=True`。
- 当前用户直接看到的状态：不在 active 选择页时 overlay 不显示完整三卡，这是预期门控结果。

### 16.3 最近推进记录

| 日期 | 本轮目标 | 改动或发现 | 证据 | 后续影响 |
| :--- | :--- | :--- | :--- | :--- |
| 2026-06-12 | 官方本地接口 go/no-go | Live Client Data / LCU 不能提供未选三张候选，只能做诊断或门控 | 局内采样无 augment/hextech 候选字段；LCU 只有大厅/控制面信息 | Track B Overwolf/GEP 成为长期主数据源候选，Track A 保留 fallback/诊断 |
| 2026-06-12 | 修 inactive 边沿与 anchor 缓存 | 补 ready -> inactive 事件覆盖；修复 anchor calibration 中毒缓存自愈 | `tools/dev_checks.py` 覆盖 `active_no_candidates` 和 anchor cache 场景 | 避免 overlay 残留旧 active，运行态校准缓存不得提交或打包 |
| 2026-06-13 | 真实视觉识别验证 | 新双通道识别能触发 active，真机三槽中 2 槽认对，第三槽贴近阈值 | 真机选择页观察：最万用的瞄准镜、钢化你心等名字正确 | 进入稳定性打磨，不再把“链路未通”当主要问题 |
| 2026-06-15 | 晚间真实选择页观察 | 悬浮窗能在游戏内出现，三张卡内容曾与实际选择项对得上 | 示例：无限循环往复、你摸不到、尤里卡 | 后续重点变为闪烁、stats 显示、选择后隐藏 |
| 2026-06-15 | 修闪烁与 stats 缺失 | host 增加短暂 active 保持；stale 预计算缓存时消费本地已有详情快照 | `overlay_hint_cache.v1.json` 变为 182 条 hint，私用统计开启 | 下一轮先真机复验，不先调算法 |

### 16.4 已发现问题与处理

| 问题 | 触发条件/症状 | 根因 | 已处理方式 | 仍需验证 |
| :--- | :--- | :--- | :--- | :--- |
| 悬浮窗闪烁 | 选择页期间窗口整块消失再出现 | `selection_scene_not_detected`、`unstable`、`selection_button_missing` 与 active 短周期切换 | host 对刚显示过的完整 active 三槽短暂保留上一帧 | 真实选择页肉眼确认是否仍闪烁 |
| 胜率/出场率缺失 | 卡片第二行无 `胜率 / 出场` | 正式预计算缓存 stale 后 overlay cache 直接降级为 `cache_missing` | stale 时优先消费本地已有详情快照，避免选择阶段同步重建 170+ 英雄详情 | 真机确认画面是否显示 stats |
| 官方接口 NO-GO | 期望读取未选三张候选 | Live Client Data / LCU 不暴露未选候选 | 保留官方接口为诊断/门控，不作为三槽 provider | 除非 Riot 接口变化，否则不重复排查 |
| 视觉指纹误识别 | 细线条图标 top3 不含真卡或置信度集中在约 0.70 | 24x24 灰度 NCC 对黑底细线条区分度不足 | 不靠盲目降阈值；后续要基于真实样本重做更高分辨率或边缘/文字辅助匹配 | 需要新的样本评估后再改算法 |
| anchor cache 中毒 | 旧校准导致 ROI 偏移或按钮检测异常 | 运行态校准缓存污染 | 加入缓存自愈和尺寸/按钮校验；明确 `overlay_anchor_calibration.v1.json` 只属 runtime state | 多分辨率/DPI 真机验收 |
| 状态汇报视角错误 | 用户问“现状”时得到文件/函数解释 | 汇报按代码层组织，没先讲功能和运行态 | 后续状态汇报先讲用户会看到什么、缺什么、何时算完成 | 持续遵守 |

### 16.5 明确不要做的方向

| 不要做 | 原因 | 允许重新打开条件 |
| :--- | :--- | :--- |
| 没有新真机证据时继续调视觉阈值 | 现有证据表明主要问题不只是阈值 | 有真实选择页样本、事件摘要和 slot 状态序列 |
| 修改 overlay 事件 schema | 当前问题可在现有事件协议内表达和定位 | 明确证明现有 schema 无法表达必要状态 |
| 提交或打包 `data/runtime/**`、`overlay_anchor_calibration.v1.json`、运行态 cache | 这些是本机运行态，不是源码事实 | 默认永不提交；只可作为对话证据摘要引用 |
| 触碰 cdragon sync、assets 或 `Augment_Icon_Manifest.json` | 当前主线是显示和识别稳定性，不是资源同步 | 用户明确切换到资源同步或新卡覆盖任务 |
| 把私用胜率/出场率包装成对外发布合规能力 | 数据来自第三方，只允许本机私用口径 | 单独进行发布合规评估 |
| 把 Track B Overwolf 混入当前 Track A 复验 | Track B 受外部注册/审核阻塞，且渲染栈不同 | Overwolf 开发者注册/审核条件变化，并明确切换主线 |
| 因 main 未同步而阻断 overlay worktree 内设计推进 | 当前执行现场以 overlay worktree 为主 | 需要发布/合入时再做主仓同步 |

### 16.6 当前主线

- 本阶段主线：真实 LoL `Borderless` 选择页复验 Track A 显示稳定性和三槽识别稳定性。
- 下一步小目标：确认晚间修复是否解决闪烁和 stats 缺失；记录选择结束后隐藏是否可靠。
- 暂不推进：Track B Overwolf、事件 schema 变更、资源同步、新卡 manifest 大改。
- 切换主线条件：Track A 真机复验通过后进入富内容显示收口；若 Track A 复验失败，先按 §15.4 / §15.5 分类定位。

### 16.7 技术路线

| 路线 | 目的 | 当前判断 | 下一步 | 阻塞/风险 |
| :--- | :--- | :--- | :--- | :--- |
| Track A Vision | Borderless 下识别并显示三卡 | 当前主线，可运行但需稳定性复验 | 真机选择页观察 + 事件轮询摘要 | ROI、抖动、误识别、DPI |
| Track B Overwolf / GEP | 获取准确三槽候选并支持独占全屏 overlay | 长期主数据源候选，当前外部阻塞 | 等 Overwolf dev 应用注册/审核条件 | 包体、分发、Riot/Vanguard 放行 |
| 富内容显示 | 胜率、出场率、英雄联动显示 | cache 与渲染链路已接入 | 真实画面确认第二行 stats 和 synergy | context freshness、私用/发布边界 |
| 样本评估 | 为后续算法调整提供证据 | 工具有基础，样本仍不足 | 收集真实帧、事件序列和 slot 状态 | 不能用旧截图盲调 |

### 16.8 人工检查点

- [ ] active 选择阶段悬浮窗是否仍可感知闪烁。
- [ ] 三张卡卡名和 tier 是否与游戏内选择项一致。
- [ ] 每张卡第二行是否显示 `胜率 xx.x% · 出场 xx.x%`。
- [ ] 当前英雄 context 存在时，synergy 是否只按当前英雄匹配；无命中时是否显示“暂无当前英雄联动”。
- [ ] 选择结束后 overlay 是否约 1 秒内隐藏，不残留上一组三卡。
- [ ] Alt+Tab 切出游戏后 overlay 是否隐藏。

### 16.9 每轮对话接续规则

每轮开始：

1. 以 overlay worktree 为主：`C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay\run`。
2. 读本节和 §15。
3. fresh probe `game_overlay_slots.v1.json`、`game_overlay_context.v1.json`、`overlay_hint_cache.v1.json`。
4. 先检查 §16.4 和 §16.5，避免重复试错。
5. 只推进当前主线的一个小目标。

每轮结束：

1. 更新当前状态、最近推进记录和下一轮入口。
2. 如果发现新坑，写入“已发现问题与处理”或“明确不要做的方向”。
3. 报告验证命令、人工检查结果和剩余风险。
4. 未经明确要求，不 stage、不 commit、不 push。

### 16.10 决策记录

| 日期 | 决策 | 原因/证据 | 是否可逆 |
| :--- | :--- | :--- | :--- |
| 2026-06-12 | 官方本地接口不作为三槽候选主源 | Live Client Data / LCU 未暴露未选三张候选 | 若 Riot 接口变化可重开 |
| 2026-06-13 | 视觉链路保留为 Track A 主线/fallback，先做稳 | 真机能触发 active 且部分槽位正确，但仍有延迟和置信度问题 | 可逆，取决于 Track B 解阻 |
| 2026-06-13 | 私用胜率/出场率允许显示 | 用户明确本机私用要显示 stats | 对外发布默认仍不可声明合规 |
| 2026-06-15 | 先真机复验晚间修复，不先调算法 | 闪烁和 stats 缺失已有对应修复，需先验证 | 可逆，若复验失败再分类定位 |
| 2026-06-16 | overlay worktree 是设计方案执行主现场 | 主仓 main 当前不同步且有其他待处理提交；实际 overlay 改动集中在 worktree | 后续发布/合入时同步 main |

### 16.11 下一轮入口

- cwd：`C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay\run`
- 当前目标：真实选择页复验 Track A 显示稳定性、三槽识别和富内容显示。
- 第一条命令或检查：

```powershell
python -m hextech.overlay --self-check
python -c "from hextech.overlay.hints import load_overlay_hint_cache; c=load_overlay_hint_cache(); print(c.get('error'), len(c.get('hints') or {}), (c.get('source') or {}).get('private_policy_stats_enabled'))"
```

- 必读文件：`run/docs/hextech_game_overlay_design.md` §15、§16。
- 不能做的事：无新证据不调阈值、不改 schema、不提交 runtime cache、不切 Track B、不把私用 stats 包装成发布合规。
- 剩余风险：当前 runtime 非 active，核心验收必须等真实海克斯选择页。

### 16.12 独立 Game Overlay 模块（2026-06-20）

- 游戏内显示与 Web 前端是两个独立产品模块，可分别启停；单开 overlay 不启动 Web、FastAPI、浏览器或 Web 端口。
- `hextech/display/desktop/service_manager.py` 只保留一个逻辑 `game_overlay` 服务，并委托 `GameOverlayController.start()/stop()`。
- `hextech.overlay` 只依赖共享 `hextech.catalog` 与 overlay 数据协议，通过 `OverlayDataSource` 收口；不得反向导入 Web/API。
- 正式绘制唯一入口是 `renderer.draw_overlay_frame()`：三个统计窗与原生卡片等宽、位于其正上方；右侧仅显示 0–3 条当前英雄真实命中联动。
- 不再绘制 summary、候选列表、cache miss、context pending、全局 banner 或诊断状态条；诊断变化仅写去重日志。
- 离线视觉证据由 `python -m tools.overlay_render_snapshot --case all --background <真机截图>` 直接写 PNG，不存在 PostScript fallback。
