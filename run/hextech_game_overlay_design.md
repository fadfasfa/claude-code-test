# Hextech 游戏内显示临时设计方案

本文是 `run/` 工作区内关于 Hextech 海克斯提示「Web 前端」与「游戏内显示」双开关能力的临时设计方案。

它只记录目标、路线、实现边界、性能预算、验收方式和待确认问题；不代表当前代码已经实现，也不替代 `PROJECT.md` 的现状说明。后续进入实现前，应先把本文转成更细的实施计划，并重新核对 Riot、Overwolf 和 Windows 捕获能力的当前官方文档。

## 1. 目标状态

目标是在现有 Hextech 伴生系统上新增两个可独立控制的运行能力：

| 开关 | 目标 | 关闭时要求 |
| :--- | :--- | :--- |
| Web 前端 | 启动现有本地 FastAPI / Web 页面，用于浏览器详情页、英雄查询、数据调试和手动查看 | 停止 Web 进程、停止浏览器联动、释放 WebSocket 和预热任务 |
| 游戏内显示 | 在 LoL 游戏内海克斯出现后，把提示显示在三张海克斯卡片下方，目标响应时间不高于 500ms | 停止截图捕获、识别循环、Overwolf overlay 和相关缓存占用 |

两个开关必须互不强依赖：

- 只开 Web 前端：保留现有浏览器和 API 使用体验，不启动游戏 overlay 和截图识别。
- 只开游戏内显示：不强制启动 Web 服务，直接读取本地轻量提示缓存，减少无用内存占用。
- 两者同开：Web 前端提供调试和详情页能力，游戏内 overlay 使用同一份预热数据或通过本地 IPC 获取更新。
- 两者全关：主 Tk 控制台保持轻量常驻，只显示状态和开关，不进行高频轮询、截图或浏览器协同。

用户已确认的硬约束：

- LoL 目标显示模式是游戏设置里的 `Full Screen`，不是仅支持 Borderless。
- Web 前端关闭时，游戏内 overlay 仍必须完全可用。
- 500ms 响应目标从「海克斯卡片开始出现」开始计算，而不是从三张卡片完全静止后开始计算。
- 最终交付形态仍要求完全独立便携包，不能把 Overwolf 作为最终必装运行时依赖。
- 游戏内显示关闭后，不完全冷停监听；保留低频监听，以便下次开关开启时更快恢复。
- 用户要求胜率/排名不能被移除；该要求与 Riot 当前公开政策存在冲突，必须作为合规阻塞风险单独处理。

## 2. 推荐技术路线

临时路线仍建议从「Overwolf Electron overlay + 本地 Vision sidecar + 现有 run 数据能力」开始做能力验证，但最终路线必须满足「独立便携包」目标。

选择原因：

- 普通 Tk、Electron、WebView2 或 Win32 置顶窗口很难稳定覆盖 LoL 的独占全屏渲染。
- Overwolf 官方能力面向游戏内 overlay，提供 exclusive overlay、overlay window options、输入穿透、窗口约束和游戏窗口信息等能力，更接近 ARAMBro、Porofessor、Mobalytics 这类视觉效果。
- 本地 Vision sidecar 可以专门负责截图和识别热路径，避免把 500ms 响应目标压在浏览器、Python UI 或网络请求上。

需要注意：Overwolf 适合验证「全屏 overlay 是否能达到目标体验」，但它本身不满足「最终完全独立便携包」约束。因此正式落地前必须增加一个可行性分叉：

| 路线 | 用途 | 是否满足最终独立便携包 | 风险 |
| :--- | :--- | :--- | :--- |
| Overwolf Electron overlay | 验证 Full Screen 显示效果、输入穿透和视觉形态 | 否 | 依赖 Overwolf 生态和分发规则 |
| 独立 Win32 / DirectComposition / Windows Graphics Capture overlay | 最终便携包候选 | 待验证 | 独占全屏下可能无法稳定覆盖游戏画面 |
| DirectX hook / DLL 注入 | 技术上可能覆盖独占全屏 | 不采用 | 反作弊和账号风险不可接受 |

阶段 0 的真实目标不是直接定死实现，而是回答这个问题：在不注入、不读内存、不依赖 Overwolf 最终运行时的前提下，是否能稳定覆盖 LoL `Full Screen`。如果答案是否定的，需要在「接受 Overwolf 依赖」「降级支持 Borderless」「放弃 Full Screen 覆盖」之间重新决策。

参考链接：

- Overwolf Exclusive Mode Overlay: https://dev.overwolf.com/ow-native/guides/product-guidelines/app-screen-behavior/exclusive-mode-overlay/
- Overwolf OverlayOptions: https://dev.overwolf.com/ow-electron/reference/Overwolf-electron-APIs/overlay/interfaces/OverlayOptions/
- Riot LoL Developer Policy: https://support-developer.riotgames.com/hc/en-us/articles/22698698001939-League-of-Legends
- Riot Vanguard FAQ: https://www.riotgames.com/en/DevRel/vanguard-faq
- Microsoft Desktop Duplication API: https://learn.microsoft.com/en-us/windows-hardware/drivers/display/desktop-duplication-api
- Microsoft Windows Graphics Capture: https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture

## 3. 现有系统切入点

当前 `run/` 已有能力：

- `hextech_ui.py` 是桌面启动兼容入口。
- `display/hextech_ui.py` 持有 Tk 主界面、后台线程和 `web_process`。
- `display/ui_runtime.py` 负责 Web 子进程启动、LCU 查询、窗口跟随和当前 Tk overlay 的显隐。
- `display/web_api.py` 已有 `/api/champion/{name}/hextechs` 等海克斯数据接口。
- `processing/` 与 `scraping/` 已有 CSV、预计算、图标、远端抓取和本地缓存能力。

关键现状约束：

- 现有 Tk UI 会在初始化时无条件启动 Web 服务；后续应改为由「Web 前端」开关驱动。
- 现有 Tk overlay 逻辑在检测到游戏窗口时隐藏；游戏内显示不应继续复用这条路径，而应独立交给 Overwolf overlay。
- 现有 Web 接口可以作为数据来源，但游戏内显示不能依赖 Web 必开，否则无法达成「单开游戏内显示以节省资源」目标。

## 4. 进程与模块拆分

建议新增或调整以下边界：

| 单元 | 形态 | 生命周期 | 职责 |
| :--- | :--- | :--- | :--- |
| Tk 控制台 | Python/Tk | 主进程常驻 | 展示两个开关、运行状态、错误摘要和手动校准入口 |
| ServiceManager | Python 模块 | 主进程内 | 统一启动/停止 Web、Overlay、Vision sidecar，并记录状态 |
| Web 服务 | Python 子进程 | Web 开关控制 | 继续提供现有浏览器前端、API、WebSocket 和数据调试能力 |
| Overlay host | Overwolf Electron | 游戏内显示开关控制 | 在游戏内渲染三张海克斯卡片下方的提示标签 |
| Vision sidecar | 原生或 Python 子进程 | 游戏内显示开关控制 | 截取 ROI、识别海克斯图标、输出稳定识别结果 |
| Hint cache | 本地轻量数据文件 | 按数据刷新生成 | 供 Web 与 Overlay 共同读取，避免 overlay 依赖 Web 服务 |

ServiceManager 至少需要提供这些动作：

- `start_web()`
- `stop_web()`
- `start_game_overlay()`
- `stop_game_overlay()`
- `get_status_snapshot()`
- `persist_user_preferences()`

开关状态应持久化到运行态配置，例如 `data/runtime/state/ui_feature_flags.json`，但该文件仍属于运行态，不作为发布源数据提交。

## 5. 双开关 UI 设计

Tk 控制台顶部建议新增两个明确开关：

- `Web 前端`
- `游戏内显示`

每个开关旁边显示三态状态：

- `未启动`
- `启动中`
- `运行中`
- `停止中`
- `异常`

交互规则：

- 切换开关时立即更新为 `启动中` 或 `停止中`，实际进程结果异步回写。
- 失败时回退开关状态并显示简短错误摘要。
- 用户关闭主窗口时，按当前开关状态停止子进程，再退出主进程。
- 默认启动状态需要用户确认；临时建议默认只启动 Tk 控制台，两个开关都关闭，最大限度节省资源。
- 游戏内显示开关关闭时，不再做高频截图和识别，但保留低频监听状态，用于判断 LoL / 选择界面是否接近可用，便于下次开关开启时快速恢复。

低频监听的目标边界：

- 不启动 overlay 渲染。
- 不执行高频 ROI 捕获。
- 不加载完整图像模板到热路径。
- 可以低频检查 LoL 进程、窗口标题、LCU 状态或轻量心跳。
- 监听频率建议 2-5 秒一次，避免与「关闭后节省性能」目标冲突。

## 6. 游戏内显示数据流

推荐数据流：

```text
后台数据刷新
  -> 生成本地 hint cache
  -> 游戏内显示开关开启
  -> 启动 Vision sidecar
  -> 启动 Overwolf overlay
  -> sidecar 捕获海克斯卡片 ROI
  -> sidecar 识别三张海克斯图标
  -> sidecar 查询本地 hint cache
  -> 通过 IPC / WebSocket 推送给 overlay
  -> overlay 更新三张提示标签
```

游戏内显示不应在热路径里做：

- 远端网页抓取。
- 大 CSV 冷读取。
- 浏览器页面加载。
- OCR 全屏扫描。
- 网络请求等待。
- 新建模型或图像索引。

这些都应提前预热，或在开关开启阶段完成。

## 7. 500ms 响应预算

硬目标：从海克斯卡片开始出现起，提示标签更新延迟不高于 500ms。

因为计时点提前到「卡片开始出现」，识别链路不能等动画完全结束。建议把内部预算压到 250-300ms，给游戏帧率、窗口合成、首次卡顿和低端机器留余量。

| 阶段 | 目标耗时 | 说明 |
| :--- | :--- | :--- |
| ROI 捕获 | 16-50ms | 只截海克斯区域，避免全屏图像处理 |
| 图标识别 | 5-40ms | 预建模板索引，优先 pHash / ORB / 多尺度模板匹配 |
| 稳定去抖 | 40-120ms | 从卡片出现早期开始判断，最多等 2-4 帧 |
| 本地提示查询 | 1-5ms | 内存 map 或轻量 JSON，不查网络 |
| IPC 推送 | 1-10ms | 本机 WebSocket、named pipe 或 Overwolf 通道 |
| Overlay 渲染 | 16-50ms | 文本和小图标，避免重排和复杂动画 |

验收指标：

- P50 延迟 <= 250ms。
- P95 延迟 <= 500ms。
- 单次识别错误不会长期停留，下一轮稳定识别应自动覆盖。
- 关闭游戏内显示后，截图和高频识别 CPU 占用应降到 0 或接近 0；低频监听允许保留，但必须有独立占用指标。

## 8. Vision sidecar 方案

第一版建议使用固定 ROI + 图标模板匹配。

输入：

- LoL 游戏窗口位置和分辨率。
- 海克斯选择界面中三张卡片的相对区域。
- 本地海克斯图标模板索引。

输出：

```json
{
  "event": "augment_choices_detected",
  "detected_at": 1710000000.123,
  "latency_ms": 184,
  "choices": [
    {
      "slot": 1,
      "augment_id": "example_augment",
      "confidence": 0.94
    }
  ]
}
```

实现建议：

- MVP 先支持 1920x1080 和 2560x1440 的常见布局。
- 提供一次手动校准入口，允许用户拖动三张卡片识别区域。
- 后续再做自动定位，例如通过 UI 边框、图标轮廓或已知卡片间距反推位置。
- 不读取游戏内存，不注入进程，不修改客户端文件。

## 9. Overlay 显示方案

Overlay 只负责渲染，不做复杂业务判断。

推荐显示内容：

- 海克斯名称。
- 一行机制提示。
- 一行适用条件或风险提醒。
- 可选小标签，例如 `连招`、`经济`、`保命`、`装备联动`。

不建议显示：

- 胜率。
- 排名。
- `必选`、`无脑拿` 这类替玩家决策的文案。
- 任何需要实时战局隐含信息推断的内容。

布局原则：

- 每张海克斯卡片下方一个紧凑 caption。
- 默认点击穿透，不阻挡玩家选择。
- 热键临时显示/隐藏，例如 `Alt+H`。
- 校准模式下才允许拖拽和点击。

## 10. Web 前端与游戏内显示解耦

为满足节省性能的目标，需要把「数据服务」和「浏览器前端」拆开理解。

建议分层：

- `Data cache`：本地文件和内存索引，低成本，可被 Web 和 Overlay 共用。
- `Web API`：需要 Web 开关开启。
- `Browser UI`：需要 Web 开关开启，并且可单独选择是否自动打开浏览器。
- `Overlay IPC`：只在游戏内显示开关开启时启动。

因此，游戏内显示单开时：

- 不启动浏览器。
- 不启动 FastAPI，除非实现阶段确认 overlay 复用 FastAPI 的成本可接受。
- 直接加载本地 hint cache 和图标索引。
- 只启动 Vision sidecar 与 Overlay host。

用户已确认：Web 前端关闭时，游戏内 overlay 仍必须完全可用。因此正式实现中必须把 overlay 热路径依赖从 Web API 中剥离出来。Web API 可以作为调试入口，但不能作为游戏内显示的必需运行时。

## 11. 合规与安全边界

必须遵守这些边界：

- 不读取或修改 token、cookie、API key、proxy secret、auth 文件。
- 不注入 LoL 进程。
- 不读取 LoL 进程内存。
- 不修改游戏客户端文件。
- 不做自动点击、自动选择或自动输入。
- 不显示 Riot 明确不批准的 Augments / Arena Mode items win rate。
- 不把运行态日志、截图缓存、profile、raw 数据作为发布源数据提交。

内容边界建议：

- 使用「机制说明」和「条件解释」降低自动决策属性。
- 对低置信度识别显示 `识别中`，不要乱给确定提示。

### 11.1 胜率/排名争议项

用户明确要求：游戏内提示不允许去掉胜率/排名。

该要求与 Riot 当前公开政策存在冲突。Riot LoL Developer Policy 明确把 Augments / Arena Mode items 的 win rate 列为不批准内容。本文不能把「显示胜率/排名」写成合规方案，也不能把它作为默认可发布能力处理。

临时处理原则：

- 在设计文档中保留用户需求，不删除该目标。
- 在实现计划中必须把胜率/排名显示列为 `policy_blocked` 或 `requires_explicit_go_no_go`。
- 如果只做本机私人实验，也必须在 UI 和文档中标注风险，不能宣称 Riot 合规。
- 默认可发布模式应当支持关闭胜率/排名；但这与用户当前硬需求冲突，需要在正式实现前再次决策。

也就是说，技术上可以为数据模型预留 `rank`、`score`、`winrate` 字段，但是否在游戏内 overlay 显示，不能在当前合规信息下直接视为通过。

## 12. 实施阶段建议

### 阶段 0: 决策验证

目标：确认两件事：Overwolf 能覆盖用户实际 LoL `Full Screen` 模式；独立便携包候选路线是否有机会在不注入的前提下覆盖同一模式。

验收：

- LoL `Full Screen` 模式下 overlay 可见。
- overlay 可点击穿透。
- overlay 可由热键隐藏和显示。
- 如果 Overwolf 可行但独立便携包路线不可行，需要把「最终独立便携包」与「Full Screen」冲突报告为 blocker。

### 阶段 1: 双开关生命周期

目标：把当前无条件启动 Web 改为可控启动。

验收：

- 只开 Web 前端时，Web API 可用，游戏内显示进程不存在。
- 只开游戏内显示时，Web 进程不存在，overlay 占位窗口可启动。
- 两者同开时状态互不覆盖。
- 两者关闭后，无 Web、overlay、截图或高频识别残留子进程；允许低频监听，但必须可见、可关、可计量。

### 阶段 2: 本地 hint cache

目标：生成 overlay 可直接读取的轻量提示缓存。

验收：

- 不启动 Web 服务也能按 `augment_id` 查询提示。
- 缓存缺失时显示明确错误，不触发远端抓取阻塞游戏内显示。

### 阶段 3: Vision MVP

目标：识别三张海克斯卡片。

验收：

- 固定分辨率下识别稳定。
- P95 从卡片完全可见到识别输出 <= 300ms。
- 低置信度不输出误导性提示。

### 阶段 4: Overlay 数据接入

目标：把识别结果显示到卡片下方。

验收：

- P95 从卡片完全可见到 overlay 文案更新 <= 500ms。
- 三个 slot 不串位。
- 关闭游戏内显示后 CPU、截图、overlay 渲染全部停止。

### 阶段 5: 性能与打包

目标：明确便携包和 Overwolf 验证包的分发边界，并判断最终独立便携包目标是否可达。

验收：

- 记录三种状态内存占用：全关、只开 Web、只开游戏内显示、两者同开。
- 记录 CPU 占用和平均识别延迟。
- 如果最终仍依赖 Overwolf，则判定不满足用户「完全独立便携包」目标。
- 如果独立便携包只能支持 Borderless，必须明确标注不满足 `Full Screen` 目标。

## 13. 风险清单

| 风险 | 影响 | 缓解 |
| :--- | :--- | :--- |
| Overwolf 对 LoL 当前版本的 exclusive overlay 支持不稳定 | 无法满足真全屏显示 | 阶段 0 先验证，不通过则降级为 Borderless-only |
| Riot 政策边界变化 | 内容可能需要调整 | 保持机制说明，不显示胜率和自动决策 |
| 图标动画导致误识别 | 显示错误提示 | 多帧去抖、置信度阈值、低置信度显示识别中 |
| 分辨率和 UI 缩放差异 | ROI 偏移 | 手动校准 + 常见分辨率预设 |
| Web 与 Overlay 共享数据不清晰 | 资源重复、内存增加 | hint cache 作为共享低成本数据层 |
| 子进程停止不干净 | 残留 CPU/内存占用 | ServiceManager 统一 stop、超时 kill、退出前状态检查 |
| 最终独立便携包与 Full Screen overlay 冲突 | 可能无法同时满足两个硬目标 | 阶段 0 单独验证，不通过则提请重新取舍 |
| 胜率/排名与 Riot 政策冲突 | 可能无法合规发布或存在账号风险 | 标记为 policy_blocked，正式实现前再次 go/no-go |

## 14. 待用户确认问题

这些问题会影响正式实施计划：

1. 已确认：LoL 目标模式是 `Full Screen`。
2. 已确认：最终仍要求完全独立便携包，不接受 Overwolf 作为最终必装运行时依赖。
3. 暂不确认：游戏内显示单开时，是否允许启动一个极小本地 IPC 服务，还是必须完全无 Web/HTTP 进程。
4. 待确认：Web 前端开关开启时，是否默认自动打开浏览器？
5. 已确认但存在合规冲突：用户要求游戏内提示不允许去掉胜率/排名。
6. 已确认：500ms 延迟从「海克斯卡片开始出现」计算。
7. 待确认：第一版是否只支持 1920x1080 / 2560x1440，并提供手动校准？
8. 已确认：关闭游戏内显示后，保留低频监听以便下次秒开。
9. 待确认：Overlay 热键默认使用 `Alt+H` 是否可接受？
10. 待确认：是否需要在 Tk 控制台显示实时性能指标，例如当前延迟、CPU、识别置信度？

## 15. 临时结论

在「LoL Full Screen 可用」和「从卡片开始出现起 500ms 内响应」这两个硬目标下，继续强化现有 Tk overlay 不是合适路线。更可行的验证路线是把现有 Tk 窗口收敛为轻量控制台，把游戏内显示先交给 Overwolf overlay 验证视觉和全屏能力，把识别热路径交给独立 Vision sidecar，并通过本地轻量 hint cache 解耦 Web 前端和游戏内显示。

但用户新增的「最终完全独立便携包」要求使 Overwolf 不能直接作为最终路线。正式实现前，第一优先级不是改业务数据，而是完成阶段 0 验证：在用户真实 LoL `Full Screen` 设置下证明 overlay 可见、可隐藏、可点击穿透，并评估独立便携包路线是否能在不注入、不读内存的前提下达成同样能力。这个验证失败时，应立即把目标冲突上报，而不是继续扩大实现。

胜率/排名显示是另一个必须前置决策的问题。用户要求保留，但 Riot 当前公开政策不支持把 Augments / Arena Mode items win rate 作为合规 overlay 内容。后续实现计划必须把它作为 `policy_blocked` 风险项处理。
