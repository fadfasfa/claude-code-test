# Hextech 游戏内 Overlay 设计方案

本文是 `run/` 工作区内 Hextech 游戏内 overlay 的当前设计口径。它记录已经收敛的 MVP 行为、模块合同、显示语义、性能预算、验收方式和剩余风险；不替代 `PROJECT.md` / `README.md`，但应与两者保持一致。

当前工作面：

- worktree：`C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay`
- 分支：`codex/feature/hextech-game-overlay`
- 目标工作区：`run/`
- 设计来源：`C:\Users\apple\.claude\plans\run-flickering-flame.md`、`run/README.md`、`run/PROJECT.md`、当前代码合同

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

当前 MVP 只承诺 LoL `Borderless` / 无边框全屏。`Full Screen` / 真独占全屏不作为当前交付能力；后续只做检测与引导，不通过注入、读内存或 Overwolf 平台绕过该限制。

## 2. 当前实现路线

当前 MVP 已收敛为：

- Tk 控制台管理用户开关与状态。
- `display/service_manager.py` 管理 Web 前端、overlay host、Vision sidecar 和低频监听生命周期。
- `display/game_overlay_host.py` 启动独立 Tk overlay host，使用 Win32 topmost / click-through / noactivate 样式覆盖 Borderless 游戏画面。
- `processing/overlay_event_channel.py` 作为本地三槽位事件协议，只读写 `data/runtime/state/game_overlay_slots.v1.json`。
- `processing/overlay_vision_sidecar.py` 作为本地 Vision sidecar，使用 Pillow/pywin32 截取 LoL 游戏窗口，先用蓝色选择按钮做场景门控，再对固定三槽位 ROI 做模板指纹匹配并写入事件文件。
- `processing/overlay_hint_cache.py` 生成 overlay 本地轻量提示缓存，避免游戏内显示依赖 Web API。
- `tools/overlay_performance_probe.py` 记录阶段 5 人工延迟样本摘要。

当前默认实现不引入 PySide/Qt、Electron、WebView2、OpenCV、imagehash、WGC 或 Overwolf。后续只有在真实 LoL 验收证明 Pillow/pywin32 + 固定 ROI 不足时，才基于采集证据评估替代捕获或识别库。

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
| `display/hextech_ui.py` | 桌面控制台 UI、开关交互、状态展示 | 不堆积子进程生命周期细节 |
| `display/service_manager.py` | 启动/停止 Web、overlay host、Vision sidecar；失败回滚；状态快照；偏好持久化 | 不截图、不识别、不渲染三槽位 |
| `display/game_overlay_host.py` | 透明置顶窗口、点击穿透、窗口跟随、`Alt+H`、三与门显隐、本地事件渲染 | 不截图、不识别、不访问远端、不依赖 Web API |
| `processing/overlay_event_channel.py` | 本地 JSON 事件协议、规范化、过期/损坏/缺失诊断、样例/假识别/inactive 事件写入 | 不做截图识别、不抓远端、不自动点击 |
| `processing/overlay_vision_sidecar.py` | `--once` 诊断探针、`--loop` 常驻识别、自门控待机、DPI awareness、事件写入 | 不读游戏内存、不注入、不修改客户端、不依赖 Web 服务 |
| `processing/overlay_hint_cache.py` | overlay 轻量提示缓存生成与按 augment_id 查询 | 不触发远端抓取阻塞游戏内显示 |
| `tools/dev_checks.py` | 离线自检、overlay 合同回归、bundle manifest 检查 | 不替代真实 LoL 人工验收 |
| `tools/overlay_performance_probe.py` | 手工延迟样本 P50/P95 摘要 | 不自动测游戏画面端到端延迟 |

## 6. 数据流

正式游戏内显示链路：

```text
Tk 控制台游戏内显示开关开启
  -> ServiceManager 启动 overlay host
  -> ServiceManager 启动 Vision sidecar --loop --preset auto --write-event
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

`processing/overlay_vision_sidecar.py` 提供两个入口：

```powershell
# 诊断探针：执行一次短窗口识别后退出
python -m processing.overlay_vision_sidecar --once --preset auto --write-event

# 正式链路：常驻自门控识别循环
python -m processing.overlay_vision_sidecar --loop --preset auto --write-event
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

### 7.2 ARAMBro / Overwolf GEP 备用方案

本节只登记第二阶段备用路线，不改变当前 MVP 默认实现。当前主线仍是本地 Vision sidecar；后续先验证 Riot / LoL 官方本地接口，只有官方接口无法提供稳定三槽候选时，才进入 ARAMBro 式 GEP 数据源验证。

只读检查 `C:\Users\apple\AppData\Local\Programs\ARAMBro\resources\app.asar` 后，ARAMBro 的关键做法可归纳为：

- 它把 Overwolf GEP 作为局内事件数据源，`LOL_GEP_REQUIRED_FEATURES` 包含 `augments`，并对 LoL / PBE game id 调用 `gep.setRequiredFeatures(...)`。
- 它监听 `new-info-update`、`new-game-event`、`game-detected` 等 GEP 事件，并每 1000ms 调用 `gep.getInfo(gameId)` 补抓当前快照。
- 它从 `owGep.stateManager.getState().augments` 读取当前候选，通过 `extractGepAugmentRefs(...)` 和 `buildGameStateSnapshotFromGepAugments(...)` 解析三张候选。
- 它的 Dev UI 暴露 `owgep:get-state`，并能从 `augments.augment_1` / `augment1`、`augment_2` / `augment2`、`augment_3` / `augment3` 读取测试槽位。
- LCU 更像是 gameflow、champ-select、summoner 等控制面来源；三张局内候选的主数据源是 GEP `augments`，评分和推荐再接本地 augment 统计数据。

如果采用该备用方案，设计边界必须是新增“候选槽位 provider”，而不是把 Overwolf 逻辑塞进 `overlay_vision_sidecar.py`。推荐边界：

```text
Overwolf GEP augments
  -> GEP bridge / provider 归一化三槽候选
  -> processing/overlay_event_channel.py 写入 game_overlay_slots.v1.json
  -> display/game_overlay_host.py 继续只按本地事件文件渲染
```

约束：

- 不导入、复用或修改 ARAMBro 安装目录代码；只把它作为已验证实现思路参考。
- 不让 overlay host 直接依赖 GEP、LCU 或 Web API；所有上游仍必须写入同一份本地三槽事件协议。
- Vision sidecar 保留为 fallback / 诊断路径；GEP provider 只能在官方接口 provider 走不通后作为优先数据源，并且必须能独立关闭。
- 采用前需要单独 go/no-go：Overwolf / ow-electron 依赖、包体、Vanguard / Riot 放行、分发方式和用户是否接受非纯便携运行时，都不属于当前 MVP 已批准范围。

## 8. Overlay Host

`display/game_overlay_host.py` 的当前设计边界：

- 根入口：`python hextech_ui.py --game-overlay` 或 `python game_overlay_host.py`。
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

离线自检：

```powershell
cd C:\Users\apple\worktrees\codex\claudecode-codex-feature-hextech-game-overlay\run
python tools/dev_checks.py
```

人工辅助命令：

```powershell
# overlay host；无 active 事件或游戏不在前台时应保持隐藏
python hextech_ui.py --game-overlay

# 写入样例事件；仍需 LoL 游戏窗口前台才会显示
python -c "from processing.overlay_event_channel import write_sample_overlay_event; print(write_sample_overlay_event())"

# 写入假识别事件，验证事件文件到三槽位渲染
python -c "from processing.overlay_event_channel import write_fake_detection_overlay_event; print(write_fake_detection_overlay_event())"

# 一次性 Vision 诊断探针
python -m processing.overlay_vision_sidecar --once --preset auto --write-event

# 正式常驻链路
python -m processing.overlay_vision_sidecar --loop --preset auto --write-event

# 写入 inactive，overlay 应隐藏
python -c "from processing.overlay_event_channel import write_inactive_overlay_event; print(write_inactive_overlay_event())"

# 手工延迟样本摘要
python tools/overlay_performance_probe.py --latency-ms 180 240 420 --source-tag manual-lol-borderless
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

胜率/排名：

- 用户需求保留：本机私用实验中仍希望保留胜率/排名能力。
- 当前项目口径：胜率/排名显示标记为 `policy_blocked` / `requires_explicit_go_no_go`。
- 默认可发布能力不得把 Augments / Arena Mode items win rate 写成合规 overlay 内容。
- 私用统计开关默认关闭；启用后必须标注风险，不宣称 Riot 合规。

## 13. 当前剩余风险

| 风险 | 影响 | 处理方式 |
| :--- | :--- | :--- |
| 真实 LoL Borderless 下 ROI 偏移 | 识别不到或 slot 串位 | 用 `--once --write-event` 收集输出后再调 ROI/阈值 |
| 真实截图置信度不足 | active 事件不稳定或错误 | 只基于真实探针数据调整模板/阈值，不盲调 |
| DPI / 多显示器差异 | 截图 bbox 与窗口位置不一致 | sidecar 已设 DPI awareness；剩余差异列入人工验收 |
| `Alt+H` 注册失败 | 无法热键切换 | 保留 warning 日志，需人工确认是否被系统/其他软件占用 |
| Borderless 覆盖不稳定 | overlay 不可见或跟随错误 | 当前 MVP 只承诺 Borderless，真实表现需训练模式验收 |
| Full Screen / 独占全屏 | 普通外部窗口无法覆盖 | 不作为当前 MVP；后续检测并引导切无边框 |
| 胜率/排名政策冲突 | 不能作为默认可发布能力 | 保持 `policy_blocked`，私用开关默认关闭 |

## 14. 当前结论

游戏内 overlay 的当前 MVP 已从“未来方案”收敛到一条明确实现线：Tk overlay host 只负责窗口、热键、跟随和渲染；Vision sidecar 负责常驻自门控识别；ServiceManager 负责生命周期；本地 JSON 事件文件负责进程间契约；本地 hint cache 负责 Web 解耦。

最新显示语义是默认不显示占位框，只有“用户开关开启 + active 海克斯选择事件 + LoL 游戏窗口前台”同时满足才显示。蓝色选择按钮检测分为两步：新环境首次定位按钮并写运行态校准缓存，后续每轮仍用固定按钮 ROI 确认选择场景存在。剩余工作不是重新选技术栈，而是在真实 LoL `Borderless` / 无边框全屏下完成人工验收，并根据实际截图证据修正 ROI、阈值和性能预算。
