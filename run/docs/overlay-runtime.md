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
- 单帧证据分为 `strong`、`medium`、`weak`。真机名称样本，或双字体高置信度且有一致图标/明确视觉版本，才是 `strong`；仅“双字体同身份且无可靠图标冲突”才是 `medium`。单字体、优势字体和图标短名单只写诊断，绝不能独立产生 `ready`。
- 槽位确认只由时序仲裁器负责：`strong` 在最近 3 个原始观察中同身份命中 2 次，`medium` 在最近 5 个原始观察中命中 3 次且窗口内没有其他 `strong` 身份。窗口按真实 `captured_at` / `recognition_completed_at` 排序，不假定固定帧率；单帧 miss、weak 候选或鼠标遮挡作为空观察占据窗口，不能跳过空帧累计旧证据，也不清空仍在 6 秒证据寿命内的累计。
- 已确认槽在当前 selection epoch 内持续保留；重随的新身份满足同一确认规则后才原子替换。相同卡名后续获得更强图标证据时只补充 `visual_variant_id` 与 tier，不递增 `selection_revision`。
- 已稳定槽只能由不同身份的 `strong` 证据替换；`medium` 只用于首次确认，避免系统性 OCR 误匹配在连续帧中覆盖已展示结果。
- 同一帧一个或多个槽发生真实替换时，`selection_revision` 只增加一次。
- Sidecar 输出 `cursor_over_slots`。鼠标遮挡的稳定槽保持原结果；未遮挡槽继续识别，不能因为一个槽被遮挡而冻结全部三槽。
- 已识别槽立即显示。选择窗口存续期间，尚未稳定、通道分歧或低 margin 的槽始终显示“识别中”；不存在固定 3 秒识别失败。只有模板索引、持续截图或 Sidecar 进程等硬故障才能进入失败态。空槽和硬故障都不能推断为来源无数据。
- 未稳定槽在至少 5 次原始观察、持续至少 2 秒且任一身份最高命中仍不足 2 次时，诊断为 `evidence_starved`；公共状态仍为 `detecting`，不得生成 `failed` 或 `RECOGNITION_MISSING`。该诊断只说明证据饥饿，不能据此全局放宽 OCR 阈值。
- 场景门丢失采用真实时间宽限：已有部分稳定槽且仍有待识别槽时保留 6 秒，三槽均 ready 时保留 1.5 秒；场景恢复立即取消宽限并沿用原 epoch、revision 和证据。明确 `selection_click` / `selection_confirmed` 后可立即结束；其他情况只有宽限耗尽才输出 `scene_loss_confirmed`。鼠标遮挡、`card_residue` 和 `name_residue` 只能进入 `scene_grace_hold`，不能按固定两帧清空稳定槽。
- `game_not_foreground`、计分板、临时最小化、短暂截图不可用和 client size 抖动使用 `transient_pause`：Host 隐藏窗口，但 Sidecar 保留当前 epoch、revision、稳定槽和证据；同一 `game_instance_id` 返回后继续识别。仅暂停期的低频 gameflow 探测明确确认结束时，才发布携带刚结束 epoch/revision 的 `gameflow_ended` 事件并清空；新游戏实例和明确选择完成也可清空。gameflow 探测在后台 daemon 线程执行并缓存结论，识别循环只读缓存不被本机 HTTP 阻塞；返回前台或换局的 reset 会作废仍在途的旧结论。
- 联动面板文字按视口高度缩放（视口为游戏窗口物理像素，天然含 DPI）：expanded 标题/正文与 compact 单行都不再使用固定像素上限；面板高度按内容自适应、受卡片上方可用空间约束，超长说明以省略号截断，空间不足时整面板隐藏。评级以 tier 色徽章展示，不再拼进标题文本。
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
| `recognition_missing` | 旧事件兼容或识别服务硬故障；普通未稳定槽保持 `detecting` | 识别中 / 识别服务异常 |
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
- 逐选择观察：`var/state/overlay_vision_timelines/selection-*.jsonl`
- 时间线轮转：最多保留最近 20 个真实 selection epoch；空闲心跳不写入、不占额度
- 默认模式：不生成 PNG
- 显式 `--diagnostic`：允许后台线程异步截图

报告 v2 记录捕获、识别完成、事件写入、Host 读取、上下文确认、绘制开始/完成、呈现、报告入队和落盘时间；`source` 兼容增加 `selection_epoch`、`selection_revision`、`scene_state`、`selection_window_active` 和 `scene_temporal_state`，去重签名包含 epoch/revision，确保重随与结束事件不会被合并。槽位同时记录 `vision_id`、名称、有限候选、`canonical_id` 和最终缺失原因；vision 事件槽不携带数据层字段，报告写入时统一合并三份来源——vision 槽给识别身份（`vision_id`）、render model 行给 `data_status`/`data_reason`/`canonical_id`（命中 hint 的规范 id）、context 给 `champion_id`，`generation_id` 缺省回退事件 source。排查延迟时按时间链路定位阶段，不用截图文件时间代替阶段证据。

Vision 时间线 schema v1 每个 observation 记录独立序号、`capture_started_at` / `captured_at` / `recognition_completed_at`、`capture_status`、会话/游戏实例、三槽文字双字体与图标 Top-1、confidence、margin、候选身份、证据等级/计数、公开状态和 revision；场景侧同时记录 `source.reason`、`scene_present`、`scene_score`、选择按钮、卡片/名称残留、鼠标覆盖槽、点击和 `scene_temporal_state`。`transient_pause` 与携带原 epoch/revision 的 `gameflow_ended` 也写入同一时间线；未截图的事件用同一 wall-clock 时间并标记 `capture_status=not_captured`，后者会产生 epoch 的 P50/P95 结束摘要；空闲心跳仍不写入。每个 epoch 的截图、识别和端到端 P50/P95 只统计 `observation_kind=recognition` 且 `capture_status=captured` 的真实观测，默认不包含图片。`vision_eval` 的稳定性门禁只回放时间戳严格递增、ID 唯一的独立 observation，完整静态帧仅验证三通道单帧候选，禁止重复同一截图制造稳定帧。

`overlay_vision_trace_history.v1.json` 仅在状态签名变化时追加；`cursor_over_cards` / `hover_occluded` 属于鼠标位置噪声，不参与签名（字段仍写入条目），避免空闲期把 256 条历史冲成无价值的空槽帧。离线分离度分析使用 `tooling/diagnostics/vision_separation.py`：读取 `overlay_vision_timelines/*.jsonl` 与人工真值，输出三通道 top1 命中率、命中/未命中的 confidence 与 margin 分布及 Cohen's d；识别阈值只允许依据该报告调整，不得盲调。

显式 diagnostic 模式为每个真实 selection epoch 连续保存前 5 个独立 observation 的按钮、三槽图标和三槽卡名 ROI，每组带 observation 序号和三项时间戳；不保存完整游戏截图，并沿用受限轮转。旧 Build 真机报告测得端到端中位数约 207.5 ms、P95 约 259.2 ms；本轮只建立真实指标，若新 Build P95 仍高于 180 ms，应单独优化热路径，不能通过放宽识别规则掩盖。

## 对局期间的数据刷新

存在可用 current generation 时，对局期间延后 Catalog、Hextech、Apex、Mayhem 的自动刷新和普通手动刷新，状态为 `refresh_state=deferred`、`deferred_reason=game_in_progress`。这不是 `data_stale`，不计入失败和 backoff。

若刷新过程中进入游戏，协调器通过既有 cancel 文件协作停止 worker，并保留 current/last-good。对局结束后等待 30 秒，只提交一次合并后的刷新请求。没有可用 snapshot 的冷启动不受此策略阻塞。冻结态摘要日志不得记录 Scrapling 单请求成功或重试流水，只保留来源开始、结束、聚合计数和有限失败样本。

## 系统托盘、轻量待机与无终端启动

- Desktop 右上角“×”和 `WM_DELETE_WINDOW` 只隐藏到 Windows 系统托盘，不停止识别或退出进程。完全退出只能使用托盘菜单“退出 Hextech”。托盘还提供“显示 Hextech”“重启识别”和只读运行状态。
- 再次点击桌面快捷方式不会启动第二套服务；新实例向当前 owner 写入 `desktop_ui_activation.v1.json` 激活请求后以退出码 0 结束，原实例显示窗口并按需恢复服务。请求必须匹配当前 `owner_id`，陈旧或其他 owner 请求会被忽略。
- 连续 300 秒既无 `LeagueClient.exe` / `LeagueClientUx.exe`，也无 `League of Legends.exe` 时进入轻量待机。客户端即使最小化，只要进程仍在就不待机；单独的 Riot Client launcher 不算 League 活动。
- 轻量待机停止 DataService、Web、Runtime Supervisor、Overlay host 和 Vision Sidecar，只保留 Desktop、托盘与 League 进程探针；探针按运行态分档——待机/恢复失败态 1 秒（保 15 秒唤醒预算），服务运行期 5 秒（仅用于 300 秒空闲判定，避免对局与大厅期间每秒全量枚举进程）。真正停止前必须二次探测 League，避免探测后到停机前的竞态；League 客户端或游戏进程出现、托盘显示、托盘重启识别或快捷方式激活会恢复。自动恢复的端到端预算为 13 秒，连同最坏一轮探测仍小于 15 秒；恢复逐项验证 DataService、Supervisor、Host、Sidecar 心跳和 Build ID，失败后 League 仍存在时每 5 秒重试，托盘“退出 Hextech”后则不再自动启动。
- `var/state/background_runtime_transitions.v1.json` 是独立 schema v1 的有界生命周期诊断，最多保留 200 条状态转换，仅记录原因、匹配进程名、组件结果和错误类型，不记录命令行或敏感信息。
- 识别运行态必须区分 `suspending`、`suspended`、`resuming`、`restart_in_progress`、`resume_failed`、`resume_cleanup_pending` 与真实 Sidecar `stale/failed`。主动待机文案为“识别已休眠”，不得显示为“识别失效”；`resume_cleanup_pending` 表示先回收上次失败的残留服务，回收未确认时不得并行启动新 Supervisor。Supervisor 发现已启用 Sidecar 存活陈旧时先发布 `starting / sidecar_restart` 并显示“识别重启中”；只有既有恢复预算耗尽后才进入最终错误态。
- PyInstaller 主程序必须使用 Windows GUI subsystem（`--windowed`）。Supervisor 与 DataService 使用带随机 token 的原子 bootstrap 文件握手；所有本程序 Python 子进程同时使用 `CREATE_NO_WINDOW`，不得依赖 `sys.stdout` 或弹出控制台。桌面快捷方式和 packaged smoke 都直接启动 EXE；便携 BAT 仅为兼容入口，可能短暂闪烁。

## 打包、部署与旧包防错

打包前运行目标测试、完整 pytest、开发门禁、Pyright 和 packaged startup smoke。普通打包只生成候选，必须明确标记“候选未部署”；构建只允许使用 manifest v3。部署器拒绝缺少构建身份或 `runtime_contracts` 不等于 Overlay v3、Sidecar v2、session report v2 的候选。

所有 runtime state 文件（含 `startup_timing.v1.json`）都携带 `build_id` 以自证来源。部署到 `C:\HextechCompanion` 后，真机测试前必须核对：

1. `Hextech伴生终端.exe` 的修改时间、FileVersion 和 SHA-256 属于本次构建。
2. `_internal\bundle_manifest.json` 的 `build_id`、源码 revision、源码指纹和契约版本完整。
3. Desktop 状态、`game_overlay_sidecar_status.json`、`game_overlay_slots.v1.json`、`game_overlay_visibility.v1.json`、`reports/overlay_sessions/latest.json` 的 `build_id` 与 manifest 一致。
4. Sidecar status 为 v2、Overlay event 为 v3、session report 为 v2。旧状态文件只能作为历史证据，不能证明新进程已启动。

部署过程不得清理 `%LOCALAPPDATA%\HextechNexus\var`、历史报告或用户数据。发现构建身份不一致时先停止验收，确认旧进程已退出并重新部署，不继续分析识别率。

正式部署只更新既有 `C:\Users\apple\OneDrive\Desktop\Hextech伴生终端.lnk` 到 `C:\HextechCompanion\Hextech伴生终端.exe`；仅处理指向稳定安装或本仓 release 的重复快捷方式。`C:\HextechCompanion.previous` 始终保留部署前的一代正式版本，作为唯一紧急回滚目录。

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
