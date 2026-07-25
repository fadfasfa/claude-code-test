# Overlay 运行、诊断与部署手册

本文是 Hextech Overlay、Vision Sidecar、会话诊断、打包部署和真机验收的长期事实源。处理游戏内显示、识别抖动、无数据、旧包误测或发布问题时必须先读本文；数据目录细节仍见 [data-layout.md](data-layout.md)，系统依赖关系见 [system-design.md](system-design.md)。

## 当前契约与兼容边界

| 载体 | 新写入版本 | 读取兼容 | 构建身份 |
| :--- | :---: | :--- | :--- |
| bundle manifest | v3 | 部署器只接受 v3 | 必须包含 `build_id`、`built_at`、`source_revision`、`source_fingerprint` 和 `runtime_contracts` |
| Overlay event | v3 | v2、v3 | `build_id` |
| Sidecar status | v2 | 旧状态可读；缺少存活字段时按降级处理 | `build_id` |
| Overlay session report | v2 | 历史报告不改写 | `build_id` |
| Host visibility status | v2 | v1、v2 | `build_id` |

冻结态的 `bundle_manifest.json` 是构建身份唯一事实源。Desktop、Sidecar、Overlay host、事件和会话报告必须显示或记录同一个 `build_id`；任一组件不一致都视为旧进程或旧包混用，不能继续把真机现象归因于当前源码。

## 识别与展示规则

- 模板缓存和权威矩阵保持连续 `float16`；Sidecar ready 前一次性建立连续 `float32` 计算镜像。同一帧三槽按 icon、primary name、alt name、observed name 分组批处理，热路径不得重复转换完整矩阵。FP32 镜像分配失败时 Sidecar 以 `vision_compute_memory_unavailable` 明确失败，不回退到混合精度慢路径。
- 单帧证据分为 `strong`、`medium`、`weak`。真机名称样本，或双字体高置信度且有一致图标/明确视觉版本，才是 `strong`；相关双字体或无冲突的高置信单字体是 `medium`；其余仅写诊断的候选是 `weak`。
- 空槽首次确认需要 `strong` 连续 2 帧或 `medium` 连续 3 帧。替换已有稳定槽一律至少连续 3 帧；旧身份仍有 strong 证据时，medium 新候选不能替换。单帧空结果、weak 候选或鼠标遮挡不会撤下稳定槽。
- 同一帧一个或多个槽发生真实替换时，`selection_revision` 只增加一次。
- Sidecar 输出 `cursor_over_slots`。鼠标遮挡的稳定槽保持原结果；未遮挡槽继续识别，不能因为一个槽被遮挡而冻结全部三槽。
- 已识别槽立即显示。尚未稳定的槽显示“识别中”；持续三秒仍失败时显示“识别失败”。空槽和识别失败都不能推断为来源无数据。
- Sidecar status 暴露 `compute_profile=float32_batched`、计算镜像字节数、预热耗时和单帧各通道耗时；异宽指纹必须直接报错，不能静默丢行。

## 分来源 freshness 与联动

- generation 的聚合 `health=degraded` 只用于 Desktop 状态页和诊断。Overlay 数字统计只读取 `source_status.hextech`：Hextech 为 `fresh` 时，即使 Apex 或 Mayhem 使用 last-good，也不得显示“上一代数据”。
- Apex 与 Mayhem 的 freshness 只影响联动区域；任一使用 `last_good` 或 `data_stale` 时，命中的联动显示“联动数据为上一代”，不会污染胜率和出场率。
- DataService 的构建顺序固定为“Hextech 统计 hints → Catalog 补全最终身份集 → 当前 generation 联动投影”。Catalog-only 海克斯可以有联动，但数字区域仍如实显示公开来源缺口。
- `overlay_hints.source.synergy_projection` v1 记录英雄、条目、唯一名称、Catalog 可解析名称、投影覆盖、含联动 hint、Catalog-only 命中和有限未解析样本。输入联动和 Catalog 可解析集合都必须非空，可解析名称投影覆盖至少 99%，且不能较 last-good 回退超过 5 个百分点。
- Overlay 只显示当前英雄与当前三槽实际候选命中的最佳联动，不常驻列出英雄的全部联动；未解析污染名称只进入报告，不显示也不补造。
- Apex 英雄详情页的“没有解析出联动”不是单独的发布结论：页面身份匹配且存在明确空态文案时记为 `confirmed_empty`，解析异常、身份缺失或无空态证据仍记为失败。全英雄 success/confirmed-empty 门禁保持不变，不能用部分发布绕过失败。
- Apex 与 Mayhem 作为同一联动 cohort 原子晋升；单侧真实失败时共同保留 last-good。恢复后允许复用与当前 Catalog、manifest 和 artifact 哈希一致的已保存候选，不得手工改写正式 pointer。

## 缺失原因与用户文案

| `data_reason` | 含义 | 用户文案 |
| :--- | :--- | :--- |
| `recognition_missing` | 图像尚未稳定识别或识别失败 | 识别中 / 识别失败 |
| `identity_unresolved` | 已有视觉候选，但无法关联 canonical 统计 ID | 无法关联统计 ID |
| `source_stat_missing` | Catalog 可识别，但公开来源没有该统计 ID | 公开来源未提供此海克斯统计 |
| `champion_stat_missing` | 来源有该 ID，但当前英雄没有样本 | 该英雄暂无此海克斯样本 |
| `context_missing` | 当前英雄上下文不可用 | 等待当前英雄 |
| `snapshot_unavailable` | 没有可消费的数据代 | 数据准备中 |

不得隐藏无统计海克斯，不得使用其他英雄、历史组合或伪造数值填补缺口，也不得仅凭旧 UI 的“无数据”文案判断来源覆盖。

## 诊断与时间链路

Host 使用单线程有界队列异步写入报告，Tk 渲染线程不执行 JSON 文件写入、历史轮转或截图。队列满时合并同状态任务并优先保留最新结果；`game_overlay_visibility.v1.json` 暴露 `report_queue_depth` 和 `report_dropped_count`。

- 会话报告：`var/reports/overlay_sessions/`
- 最新报告：`var/reports/overlay_sessions/latest.json`
- 历史：最多保留 20 个 `overlay-session-*.json`
- 真实会话证据：`var/state/session_evidence/`
- 默认模式：不生成 PNG
- 显式 `--diagnostic`：允许后台线程异步截图

报告 v2 记录捕获、识别完成、事件写入、Host 读取、上下文确认、绘制开始/完成、呈现、报告入队和落盘时间；槽位同时记录 `vision_id`、名称、有限候选、`canonical_id` 和最终缺失原因。排查延迟时按时间链路定位阶段，不用截图文件时间代替阶段证据。

## 对局期间的数据刷新

存在可用 current generation 时，对局期间延后 Catalog、Hextech、Apex、Mayhem 的自动刷新和普通手动刷新，状态为 `refresh_state=deferred`、`deferred_reason=game_in_progress`。这不是 `data_stale`，不计入失败和 backoff。

若刷新过程中进入游戏，协调器通过既有 cancel 文件协作停止 worker，并保留 current/last-good。对局结束后等待 30 秒，只提交一次合并后的刷新请求。没有可用 snapshot 的冷启动不受此策略阻塞。冻结态摘要日志不得记录 Scrapling 单请求成功或重试流水，只保留来源开始、结束、聚合计数和有限失败样本。

## 系统托盘、轻量待机与无终端启动

- Desktop 右上角“×”和 `WM_DELETE_WINDOW` 只隐藏到 Windows 系统托盘，不停止识别或退出进程。完全退出只能使用托盘菜单“退出 Hextech”。托盘还提供“显示 Hextech”“重启识别”和只读运行状态。
- 再次点击桌面快捷方式不会启动第二套服务；新实例向当前 owner 写入 `desktop_ui_activation.v1.json` 激活请求后以退出码 0 结束，原实例显示窗口并按需恢复服务。请求必须匹配当前 `owner_id`，陈旧或其他 owner 请求会被忽略。
- 连续 300 秒既无 `LeagueClient.exe` / `LeagueClientUx.exe`，也无 `League of Legends.exe` 时进入轻量待机。客户端即使最小化，只要进程仍在就不待机；单独的 Riot Client launcher 不算 League 活动。
- 轻量待机停止 DataService、Web、Runtime Supervisor、Overlay host 和 Vision Sidecar，只保留 Desktop、托盘与 5 秒进程探针。League 进程出现、托盘显示、托盘重启识别或快捷方式激活会恢复；手动唤醒后重新计算完整 5 分钟空闲窗口，恢复 Web 时不重复打开浏览器。
- 识别运行态必须区分 `suspending`、`suspended`、`resuming`、`restart_in_progress`、`resume_failed` 与真实 Sidecar `stale/failed`。主动待机文案为“识别已休眠”，不得显示为“识别失效”。
- PyInstaller 主程序必须使用 Windows GUI subsystem（`--windowed`）。Supervisor 与 DataService 使用带随机 token 的原子 bootstrap 文件握手；所有本程序 Python 子进程同时使用 `CREATE_NO_WINDOW`，不得依赖 `sys.stdout` 或弹出控制台。桌面快捷方式和 packaged smoke 都直接启动 EXE；便携 BAT 仅为兼容入口，可能短暂闪烁。

## 打包、部署与旧包防错

打包前运行目标测试、完整 pytest、开发门禁、Pyright 和 packaged startup smoke。构建只允许使用 manifest v3；部署器拒绝缺少构建身份或 `runtime_contracts` 不等于 Overlay v3、Sidecar v2、session report v2 的候选。

部署到 `C:\HextechCompanion` 后，真机测试前必须核对：

1. `Hextech伴生终端.exe` 的修改时间、FileVersion 和 SHA-256 属于本次构建。
2. `_internal\bundle_manifest.json` 的 `build_id`、源码 revision、源码指纹和契约版本完整。
3. Desktop 状态、`game_overlay_sidecar_status.json`、`game_overlay_slots.v1.json`、`game_overlay_visibility.v1.json`、`reports/overlay_sessions/latest.json` 的 `build_id` 与 manifest 一致。
4. Sidecar status 为 v2、Overlay event 为 v3、session report 为 v2。旧状态文件只能作为历史证据，不能证明新进程已启动。

部署过程不得清理 `%LOCALAPPDATA%\HextechNexus\var`、历史报告或用户数据。发现构建身份不一致时先停止验收，确认旧进程已退出并重新部署，不继续分析识别率。

## 真机验收

- 首次可见反馈 P95 ≤ 300 ms。
- 正常三槽统计 P95 ≤ 900 ms。
- 单帧捕获加识别 P95 ≤ 180 ms，FP32 镜像预热 ≤ 3 秒，Sidecar 峰值工作集 ≤ 1.5 GB。
- 瞬时错误候选不得撤下稳定槽，鼠标只冻结实际覆盖槽。
- Hextech fresh 时不得因 Apex/Mayhem last-good 显示“上一代数据”；复仇焰魂命中“虚幻武器”等已知方案时必须显示当前英雄联动。
- 默认会话不得生成 PNG。
- 游戏期间不得存在 acquisition worker 或 Scrapling 逐请求摘要日志。
- EXE、manifest、Desktop、Sidecar、Overlay host 和 session report 的 `build_id` 必须一致。

单元测试和 packaged smoke 只能验证状态机、契约和进程入口；真实 League 窗口下的捕获耗时、模板区分度和 P95 指标仍需部署新包后连续真机验证。
