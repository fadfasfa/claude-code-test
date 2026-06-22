# 虎牙海克斯工具借鉴审查

本文记录对虎牙海克斯工具的静态审查结论，以及它对当前 overlay 分支的可借鉴方向。本文只作为研究报告，不修改代码、不调整识别阈值、不改变事件协议，也不把 Overwolf 路线删除。后续若要执行动态观察或实现重构，需另起明确任务。

## 1. 结论摘要

- 虎牙工具最值得借鉴的不是“识别算法”，而是产品形态：自定义协议拉起、客户端轻壳承载网页小程序、游戏检测与数据采集代理隔离、游戏内信息贴近原生卡片显示。
- 当前 overlay 分支应优先学习“卡片内嵌/贴卡显示”的 UI 组织方式。右侧联动框现阶段已经够用，暂不作为重构重点。
- LCU 与 Live Client Data 仍不能稳定提供“未选择前的三张候选海克斯”。它们适合做阶段门控、当前英雄上下文、局况数据和选定后的确认，不应再被当成三槽候选主源。
- 虎牙若能稳定显示候选，更可能是远程小程序 JS 综合本地 Riot 接口、客户端状态和自身服务侧数据后的结果；当前静态证据不足以证明它有本地识别引擎，也不足以证明它只靠 Riot 官方接口拿到未选候选。
- Overwolf 路线保留为未来增强。当前阶段不推进 Overwolf，也不把它混入 Track A 复验或本次 UI/数据研究。

## 2. 已确认静态证据

### 快捷方式入口

桌面快捷方式 `C:\Users\apple\OneDrive\Desktop\海克斯工具.lnk` 指向：

```text
TargetPath: C:\Program Files (x86)\HuyaClient\Huya.exe
Arguments : huyapc://huyaaction=http://m.huya.com?hyaction=lolhextech
WorkDir   : C:\Program Files (x86)\HuyaClient
```

这说明虎牙不是直接打开普通网页，而是用 `Huya.exe` 处理 `huyapc://` 自定义协议，并带 `hyaction=lolhextech` 进入指定小程序页面。对 Hextech 的借鉴点是可以有 `hextech://` 或桌面快捷方式直达“游戏内显示/overlay 开关页”，但这属于体验入口，不是数据来源。

### 安装模块形态

虎牙客户端目录中可见以下相关模块：

- `CefSharp.*`、`HuyaApplet.*`、`HuyaFX.WebView*`、`node.dll`：说明 UI/小程序承载更像网页壳或网页运行时。
- `Huya.AppletProxy.*`、`Huya.AppletLiveProxy.*`、`Huya.AppletRpcProxy.*`：说明小程序与本地客户端能力之间有代理层。
- `Huya.GameCenter.*`、`Huya.GameServerProxy.*`：说明游戏检测或游戏相关状态被拆成独立模块。
- `HuyaWidget.*`、`HyWidget.exe`、`HuyaSkin.*`：说明桌面悬浮窗和主题能力是独立产品层。

这些证据支持“轻壳 + 小程序 + 本地代理 + 悬浮窗”的架构判断，但不等于可复制其闭源实现。

## 3. UI 与游戏内显示借鉴

### 借鉴方向

当前 overlay 分支已有 `game_overlay/renderer.py`，并有三张卡的相对区域配置：

```text
CARD_X_RANGES = ((0.195, 0.385), (0.405, 0.595), (0.620, 0.810))
CARD_Y_RANGE  = (0.17, 0.68)
SYNERGY_X_RANGE = (0.825, 0.992)
```

这说明当前渲染层已经能围绕 LoL 三张卡和右侧区域做定位。下一步 UI 研究应优先转成“贴卡内嵌显示”：

- 每张卡的核心统计信息放到对应原生卡片内部或贴近卡片内部的低遮挡区域。
- 卡名、tier、胜率、出场率作为卡内核心信息，避免再像独立浮窗那样占据三张卡上方的额外层。
- 右侧联动框暂时保持当前方向，只在已有真实命中时显示 0 到 3 条联动，不在本轮重新设计。
- 视觉风格向 LoL 原生面板靠拢：深色半透明底、金色/棱彩/银色 tier 边、低噪声文字层级、少用诊断 UI。

### 不建议本阶段做的事

- 不把 Tk 立刻迁到 WebView2。WebView2 可以后续做 spike，但这次的关键是卡内嵌布局与数据源稳定性，不是替换渲染技术。
- 不继续扩大右侧联动框。用户当前满意，重构重点应放在三张卡本身。
- 不把虎牙 UI 逐像素复刻。只借鉴“信息贴近卡片”和“网页壳式表达”，保持 Hextech 自己的本地数据和合规边界。

## 4. 数据获取判断

### 三类数据源分工

| 数据类型 | 当前建议来源 | 用途 |
| :--- | :--- | :--- |
| 未选前的三张候选 | Track A 视觉 | 核心壁垒，仍是唯一非 Overwolf 可行候选源 |
| 阶段和场景门控 | LCU gameflow + 视觉按钮 | 避免单靠蓝色按钮导致坐标漂移或截图失败时哑火 |
| 当前英雄和局况 | LCU + Live Client Data | 当前英雄上下文、选定后局况、已选后确认 |

### 官方接口边界

当前分支的设计记录已经确认：

- Live Client Data 采样中没有稳定暴露未选三张候选字段。
- LCU 主要给 gameflow、champ-select、summoner 等控制面信息，也没有候选字段。
- `processing/official_overlay_provider.py` 仍以 `candidates_ready / active_no_candidates` 探测候选为主，这和新路线不完全匹配。

因此后续若继续改官方接口，应把它从“候选 provider”降级/改造成“官方状态与局况 source”：

- LCU 负责输出 `gameflow_phase`、`session_state`、`champ_select`、`current_summoner`、当前英雄上下文。
- Live Client Data 负责输出 `activeplayer`、`playeritems`、`playerlist`、`gamestats`、已选/已激活 augment 相关诊断。
- 这些状态通过独立 sidecar 或状态文件服务 overlay，不直接进入 renderer。

### 对虎牙数据稳定性的判断

静态证据只能确认虎牙有网页小程序壳和本地代理模块，不能确认它如何拿到未选候选。合理假设有三种：

1. 远程小程序 JS 轮询 Riot 本地接口和虎牙本地代理，做阶段/局况/已选后状态聚合。
2. 虎牙服务端或小程序端维护了额外映射，结合客户端状态展示推荐信息。
3. 候选仍可能来自某个我们未观察到的前端运行态字段或非公开通道。

当前不应把“虎牙稳定显示候选”直接等同于“Riot 官方接口可提供未选候选”。这需要动态观察验证。

## 5. 动态黑盒观察方案

本方案只作为后续任务计划。本轮不执行，不启动虎牙或 LoL，不抓包，不读取任何凭据文件。

### 观察边界

- 只观察进程树、窗口、命令行参数、模块加载、loopback 请求、公开 HTTP/HTTPS 端点响应形状。
- 不读取或保存 token、cookie、auth、API key、proxy secret、浏览器 session、`auth.json`、`.env`、`local.yaml`、`proxies.json`。
- 不反编译闭源 DLL，不 patch 客户端，不注入，不修改虎牙或 LoL 文件。
- 动态观察产物只记录脱敏摘要：时间、进程名、端点路径、状态码、字段名、是否出现候选字段。

### 推荐步骤

1. 启动前记录基线：`Huya*`、`LeagueClientUx.exe`、`League of Legends.exe` 进程是否存在。
2. 通过快捷方式或等价命令启动虎牙海克斯工具，记录新增进程树和命令行参数。
3. 记录虎牙加载的小程序 URL、页面资源域名和关键 JS 文件名，但不保存完整网页源码到仓库。
4. 在未进入游戏、选人阶段、游戏内非选择阶段、海克斯三选一阶段、选择后五个阶段分别记录 loopback 请求摘要。
5. 对 `127.0.0.1:2999` Live Client Data 只记录 endpoint、状态码、顶层字段和是否出现 augment/hextech/candidate 相关字段。
6. 对 LCU 只记录 endpoint、状态码、脱敏字段名和 gameflow 状态；不得输出 Basic Auth token。
7. 对虎牙本地代理端口只记录路径和字段名，若响应含 token/auth/cookie 字样直接脱敏或跳过。
8. 对比选择前与选择后：如果只在选择后出现 augment 字段，则证明它不能替代视觉候选源；如果选择前出现三槽候选字段，再单独评估来源和合规边界。

## 6. 后续重构建议

### 第一优先级：更新设计口径

先把当前设计里的口径从“Track B Overwolf 为现行优先数据源”调整为：

- Track A 视觉仍负责未选候选。
- 官方接口负责门控、当前英雄、局况和已选后确认。
- Overwolf 暂不推进，但作为未来独占全屏和候选源增强保留。
- 虎牙只作为 UI/运行方式参考，不作为可复制的数据源事实。

### 第二优先级：官方状态 sidecar

新增或重塑官方接口模块时，不再以三槽候选为目标，而是提供稳定状态：

- 输出 gameflow、是否 InProgress、当前英雄上下文、Live Client 可用性、已选后 augment 诊断。
- 与现有 Vision sidecar 并列，由 lifecycle/controller 管理，崩溃不影响 overlay host。
- renderer 仍只读 event/hint/context，不直接访问 LCU 或 Live Client。

### 第三优先级：卡片内嵌显示 spike

在不改识别逻辑的前提下，用离线截图或 fixture 验证卡内嵌布局：

- 三张卡内部显示 stats/tier/name 的遮挡风险。
- 1366x768、1920x1080、2560x1600 下文字是否放得下。
- 右侧联动框不动，避免一次改动范围过大。
- 若 Tk Canvas 难以达到虎牙式质感，再单独规划 WebView2 渲染 spike。

### 暂不推进

- 不推进 Overwolf 当前实现。
- 不迁移 WebView2。
- 不调整视觉阈值或识别算法。
- 不改事件 schema。
- 不触碰运行态缓存、anchor calibration、资源同步或新卡 manifest。

## 7. 实施顺序建议

1. 写设计补丁：把 `hextech_game_overlay_design.md` 中 Track B 和官方接口口径改到上述分工。
2. 写动态观察任务单：明确观察命令、脱敏规则、保存位置和退出条件。
3. 做官方状态 source 设计：替代当前“候选 provider”叙事，但保留旧 probe 作为接口变化探针。
4. 做卡片内嵌离线 mock：先基于 renderer fixture 或截图输出，不接真实识别循环。
5. 有真实观察证据后，再决定是否实现官方状态 sidecar 或 WebView2 spike。

## 8. 风险与约束

- 虎牙闭源实现不能直接复制，也不能通过反编译闭源 DLL 获得实现细节。
- Riot / Vanguard / 第三方工具合规边界仍需保守：本工具不读内存、不注入、不自动点击、不修改客户端。
- 本机私用 stats 与可发布版本必须继续区分，不能把第三方胜率数据包装成公开合规能力。
- 任何动态观察都可能接触 token 或 session 字段，必须默认脱敏并避免把原始响应写入仓库。
