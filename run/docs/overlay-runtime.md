# Overlay 运行、诊断与部署手册

本文是 Hextech Overlay、Vision Sidecar、会话诊断、打包部署和真机验收的长期事实源。处理游戏内显示、识别抖动、无数据、旧包误测或发布问题时必须先读本文；数据目录细节仍见 [data-layout.md](data-layout.md)，系统依赖关系见 [system-design.md](system-design.md)。

2026-09-10 r12 桌面改为原生owner=0的独立Tk面板，按统一可信RCLIENT主窗选择结果视觉跟随；条件置顶、失败原因和冻结桌面呈现门按[桌面专项合同](desktop-stable28.md)执行。禁止重新引入跨进程owner绑定。旧r11游戏内行为保留；桌面测试不证明真实游戏定位或识别通过。

2026-09-10 r11 深度增量见 [overlay-r11-deep-repair.md](overlay-r11-deep-repair.md)：桌面改为仅客户端前台持有条件置顶，游戏内增加先于身份的碎片否定与同帧两阶段反馈，显式有界诊断默认关闭。位置校准仍须同规格真实证据，不能由新包smoke代验。

2026-09-09 r10 联合回归增量见 [overlay-r10-regression.md](overlay-r10-regression.md)。默认桌面策略现为 `client_foreground`，不再把选人或数据准备作为窗口显示前置条件。

## 当前契约与兼容边界

2026-09-07 游戏内显示 v3 增量及现存发布阻塞见 [overlay-display-repair-v3.md](overlay-display-repair-v3.md)。
2026-09-09 增量采用 MSS 真正局部捕获、有效像素校验和已确认保留场景继续取证；几何/绘制版本使旧呈现回调失效。自动验证不替代真实五局和四规格内框校准。
该增量保留统计坐标基线待真实内框校准；生产 OCR 的低优先级等待可被新生产请求唤醒，
评价清洗只生成独立展示摘要，不修改原文身份、统计值或来源时效。

| 载体 | 新写入版本 | 读取兼容 | 构建身份 |
| :--- | :---: | :--- | :--- |
| bundle manifest | v3 | 部署器只接受 v3 | 必须包含 `build_id`、`built_at`、`source_revision`、`source_fingerprint` 和 `runtime_contracts` |
| Overlay event | v3 | v2、v3 | `build_id` |
| Sidecar status | v2 | 旧状态可读；缺少存活字段时按降级处理 | `build_id` |
| Overlay session report | v2 | 历史报告不改写 | `build_id` |
| Host visibility status | v2 | v1、v2 | `build_id` |
| Vision timeline | v2 | v1 与身份不明文件永久只读 | `build_id`、Sidecar PID/instance |

冻结态的 `bundle_manifest.json` 是构建身份唯一事实源。Desktop、Sidecar、Overlay host、事件和会话报告必须显示或记录同一个 `build_id`；任一组件不一致都视为旧进程或旧包混用，不能继续把真机现象归因于当前源码。

Host visibility status v2 可选增加独立的 `game_window_mode`，字段为 `status=supported|unsupported|unknown|error`、`mode=borderless|windowed|fullscreen|unknown`、`reason`、`source=game_cfg` 和 `observed_at`；不得复用表示 compact/expanded UI 的 `display_mode`。Overlay session report v2 原样复制该对象，旧 v2 读取方继续兼容字段缺失。游戏 EXE、配置或权限身份不明时必须 fail closed，不能把未知状态解释成支持。

## 识别与展示规则

- 生产候选不是完整资源 Catalog，也不是“有非空统计”的子集。每个数据 generation 都从 metadata 的 `enabled=true` 身份构建 `overlay_hints.source.production_augment_pool` v1；旧 206 个 ID 只作为迁移 fixture。2026-08-13 当前已验证 marker 为 245 条 metadata、237 个启用身份、8 个禁用身份；相对上一 236 池新增 2140/2143，7008 转为禁用。上游 marker 变化后数量允许继续动态变化，任何具体数量都不能成为永久硬编码门槛。
- `production_augment_pool` 中每个数字 `canonical_id` 必须唯一映射一个文字身份和至少一个已验证视觉 variant；`null` 统计身份仍留在识别池，禁用身份与 Arena、Weapon、Stat、Strawberry、Special 等审计资源不进入生产矩阵。共享图标必须标记歧义，图标通道不得单独授权 `ready`。
- Vision 模板只读取启动时绑定的 production pool 与 Catalog generation。pool 不可用、缺 ID、重复 ID、名称冲突、observed-name exemplar 无法唯一绑定或资产摘要不一致时 fail closed，保持“识别中/识别服务异常”；禁止回退到完整 Catalog 名称矩阵。Sidecar status v2 以 `vision_pool_generation_id`、`catalog_generation_id`、`production_pool_id/state/count`、`rank_identity_count` 和各通道 `matrix_rows` 证明该绑定，`stats_generation_id` 明确留空并标记为 Host 所有；旧 `data_generation_id` 仅兼容表示 Vision pool，不得再解释为统计代际。
- ARAMKit 四字段 marker 与共享绝对时效共同决定英雄总体及 Stage 1–4 是否刷新：marker 相同只允许在 `last_success_at`（缺失时回退 manifest `completed_at`）年龄不超过 5 小时时复用，时间非法或超过 5 小时必须完整抓取；marker 变化时 Catalog、ARAMKit、Blitz、Apex、Mayhem 全部进入同一刷新周期。Overlay 优先显示当前英雄当前 Stage 的 ARAMKit 胜率/出场率，缺 Stage 行时回退同英雄 `all`，再缺失才显示 Blitz tier；Blitz 不提供胜率、选择率或样本量，禁止从 tier 推断百分比。production pool 仍只由同一 Catalog generation 的 `augment_assets.v1.json` 决定，任何统计来源都不参与 eligibility。generation 发布前逐 ID 核对 pool 的 `canonical_id`、路径和 SHA-256；即使 marker probe 网络失败，也不能晋升混合代。
- 模板缓存和权威矩阵保持连续 `float16`；Sidecar ready 前一次性建立连续 `float32` 计算镜像。同一帧三槽按 icon、primary name、alt name、observed name 分组批处理，热路径不得重复转换完整矩阵。FP32 镜像分配失败时 Sidecar 以 `vision_compute_memory_unavailable` 明确失败，不回退到混合精度慢路径。
- 单帧证据分为 `strong`、`medium`、`weak`。人工真值 exemplar，或两套字体指向同一 canonical ID、置信度均至少 0.92、较小 margin 至少 0.01 且较大 margin 至少 0.025，才是 `strong`；margin 为 0 的共享/并列 icon 只作诊断，高置信且 margin 至少 0.03 的明确冲突 icon 会阻断 strong-dual 快速确认。低于 strong-dual 门槛但两字体仍一致的候选保留为 `medium`，不能被一次 icon 误判清空。两字体 Top-1 分歧时，只有双方 Top-3 中存在唯一共同身份、两路置信度均至少 0.78、各自距 Top-1 不超过 0.08 且共同候选合并分数领先次名至少 0.04，才产生 `medium`。另一个窄路径要求其中一套文字与 icon 同名、文字 `confidence >= 0.78/margin >= 0.005`、icon `confidence >= 0.75/margin >= 0.05`，且另一文字通道身份不同并处于 `margin <= 0.01` 的近并列；它也只产生需要 3/5 的 `medium`。除此以外，单字体、优势字体和图标短名单只写诊断，绝不能独立产生 `ready`。
- 槽位确认只由时序仲裁器负责：`strong` 在最近 3 个原始观察中同身份命中 2 次，`medium` 在最近 5 个原始观察中命中 3 次且窗口内没有其他 `strong` 身份。窗口按真实 `captured_at` / `recognition_completed_at` 排序，不假定固定帧率；单帧 miss、weak 候选或鼠标遮挡作为空观察占据窗口，不能跳过空帧累计旧证据，也不清空仍在 6 秒证据寿命内的累计。
- 同一帧同一身份出现在多个槽时，只有唯一 `strong` 候选可以进入时序窗口，其余重复候选按空观察处理；没有唯一 `strong` 或出现多个 `strong` 时全部抑制。已经稳定在一个槽的身份不能再在其他槽累计出第二个 `ready`。
- 已确认槽在当前 selection epoch 内持续保留。普通模板候选或感知指纹漂移没有换卡授权，无论重复多少帧都不得撤下 last-good；相同卡名后续获得更强图标证据时只补充 `visual_variant_id` 与 tier，不递增 `selection_revision`。
- 稳定槽只接受三类明确 transition：Sidecar 的独立线程约每 10 ms 捕获左键物理 down-edge，容量 32 的内存队列把 750 ms 内、同游戏窗口/实例/selection epoch 且唯一命中 production slot geometry 的事件映射为 `async_mouse_down`；`flat_crop/content_absent` 必须在 2 秒内由两个不同 `frame_id` 确认后进入 detecting；与稳定身份不同的 OCR exact 则在 last-good 背后累计 3/5，确认时才原子替换。鼠标事件只消费一次，Alt-Tab、计分板、窗口/游戏实例变化、场景结束、槽外坐标和超时事件全部丢弃；只有命中槽进入新 `slot_generation`，其他两槽保持身份与 generation。点击/内容消失后的新模板身份仍按 strong 2/3 或 medium 3/5 确认；超过 2 秒未确认新身份时恢复 last-good，但 `slot_generation` 不倒退。
- 每个槽独立维护单调 `slot_generation`，并把 session、selection epoch、slot index、slot generation、无损 RGB SHA-256、感知 fingerprint 与 captured frame ID 一起绑定到生产 OCR 证据。未 READY 槽不得按指纹跳过识别，已 READY 且指纹未变的槽才可跳过矩阵投影。
- transition 只增加 `slot_generation`；`selection_revision` 只在新身份最终确认时增加。同一帧一个或多个槽发生真实替换时，revision 只增加一次，未变化槽的身份、generation 和展示都保持不变。
- Sidecar 输出 `cursor_over_slots`。鼠标遮挡的稳定槽保持原结果；未遮挡槽继续识别，不能因为一个槽被遮挡而冻结全部三槽。
- 已识别槽立即显示。选择窗口存续期间，尚未稳定、通道分歧或低 margin 的槽始终显示“识别中”；不存在固定 3 秒识别失败。只有模板索引、持续截图或 Sidecar 进程等硬故障才能进入失败态。空槽和硬故障都不能推断为来源无数据。
- 未稳定槽在至少 5 次原始观察、持续至少 2 秒且任一身份最高命中仍不足 2 次时，诊断为 `evidence_starved`；公共状态仍为 `detecting`，展示“识别未确认”，不得生成 `failed` 或 `RECOGNITION_MISSING`。该诊断只说明证据饥饿，不能据此全局放宽 OCR 阈值。
- 碎片硬拦截从清理后的文字 mask 提取最右两个相邻、尺寸一致的有效字形，不再按整段名称宽度比例裁切。至少两槽达到既有强阈值才进入 `body_shard`；命中后在当前 selection epoch 内 latch，输出 `active=false`、`ready_slots=0`、`reason=body_shard_only` 并清空本 epoch 普通海克斯候选，场景确认结束前不得恢复普通识别。动画粘连块、淡出残字和单字不能作为后缀证据。
- 场景门丢失采用统一 0.75 秒真实时间宽限，未满三槽与三槽均 ready 不再使用不同延迟；场景恢复立即取消宽限并沿用原 epoch、revision 和证据。已经进入 active 后，若选择按钮仍存在且卡面或名称仍有残留，必须进入 `scene_button_hold`，不得启动宽限或输出 `scene_loss_confirmed`。按钮消失后才开始 0.75 秒计时；明确卡面点击或 `selection_confirmed` 立即结束，重随按钮点击只为目标槽开启 replacement。`scene_grace_hold` 内未遮挡、尚未 READY 且名称仍可见的槽继续累计本帧有效候选，candidate miss 不冲掉 pending window；稳定槽和鼠标遮挡槽冻结。
- `game_not_foreground`、计分板、临时最小化、短暂截图不可用和 client size 抖动使用 `transient_pause`：Host 隐藏窗口，但 Sidecar 保留当前 epoch、revision、稳定槽和证据；同一 `game_instance_id` 返回后继续识别。仅暂停期的低频 gameflow 探测明确确认结束时，才发布携带刚结束 epoch/revision 的 `gameflow_ended` 事件并清空；新游戏实例和明确选择完成也可清空。gameflow 探测在后台 daemon 线程执行并缓存结论，识别循环只读缓存不被本机 HTTP 阻塞；返回前台或换局的 reset 会作废仍在途的旧结论。
- 外部 layered-window Overlay 只支持 `Borderless` 和 `Windowed`。窗口探针按当前 `League of Legends.exe` PID 取得真实 EXE，配置路径固定为 `Path(executable).parent / "Config" / "game.cfg"`；WeGame 的实际形态是 `...\Game\League of Legends.exe` 对应 `...\Game\Config\game.cfg`，不能误取 EXE 父目录的上一级 `...\Config\game.cfg`。`WindowMode=0/1/2` 分别解释为 `fullscreen/borderless/windowed`；缺失、解析失败、权限失败和未知值均为 `unknown/error` 并 fail closed。探针以游戏进程身份、配置大小和 `mtime_ns` 缓存，最多每秒复核一次，16 ms 识别循环不得重复读盘。Full Screen 或 unknown 时 Host 以 `unsupported_fullscreen_mode` / `game_window_mode_unknown` 降级并隐藏，Sidecar 在截图前非破坏性 pause，不截图、不识别、不清空当前 epoch/稳定槽；只有明确确认 Full Screen 时 Desktop 才持续提示“游戏当前为全屏模式，请在游戏内视频设置切换为无边框”，unknown 只保留结构化状态、有限日志和诊断，不持续占用用户状态栏。程序不得修改 `game.cfg`、不得触发 Web fallback，也不要求重启；用户切换为 Borderless/Windowed 后下一次探针更新自动恢复原游戏实例和上下文。
- 显示使用固定客户区版式 `fixed_card_layout_v2`，不再消费逐帧 `layout_transform`、按钮宽度、OCR 或 READY 数量。参考为指引关闭的 2560×1600 真机帧，统计位于各卡片下部留白并整段居中（中心约 952px）；16:9/16:10 共用卡槽横向比例，纵向按各自版式。正式适配 1920×1080、1920×1200、2560×1440、2560×1600，分别覆盖 DPI 100/125/150/175/200%，1280×720 保留兼容回归；其他比例标记为未验收。统计基础字号按卡框比例和格式上界一次确定，不按当前值缩放；Tk 使用负像素字号，DPI 不重复放大。长状态在固定统计条内换行，compact 联动使用固定 96px 参考高度，expanded 使用卡片上方固定可用区域，内容只换行或省略，不驱动框高。生产 Tk 与离线 Canvas 使用实际字体测量；窗口位置、客户区和 DPI 自动读取，截图支持副屏负坐标，不依赖品牌/英寸/EDID。
- 每段可见文字只创建一个 Canvas text item，不绘制偏移黑色副本文字；可信 Context Broker publication 通过游戏实例、窗口、进程时间和 publication 序号校验后首 tick 即可渲染。Live Client 优先读取 `/allgamedata` 的 `gameData.gameTime`，`activeplayer` 只作无 epoch 证明的内容回退；游戏 epoch 优先使用 `PID + process_started_at`，身份不可用时才退到窗口。新 epoch 立即撤下旧英雄，只有有限 `gameTime <= process age + 30s` 或仍在 120 秒内且来自上一游戏窗口消失后的本局选人 ticket 能证明归属；两路有效但英雄冲突时 fail closed。无法证明时发布空英雄和 `context_game_epoch_unconfirmed`，冲突发布 `context_source_conflict`，不得用英雄是否变化猜测换局。
- Host 按 session、selection epoch/revision、逐槽 generation/身份、Context revision、整局固定的 `stats_generation_id`、Stage/scoped run、布局、viewport 和 DPI 计算语义键；`vision_pool_generation_id` 只证明识别矩阵来源，不参与统计换代。confidence、时间戳和 evidence hit 等诊断噪声不参与；相同语义键跳过 projector 与 Canvas 重绘。Stage 变化可以更新展示但不增加 Vision slot revision；新槽尚在识别时先呈现轻量 detecting shell，同一 epoch 未变化槽复用 last-good 身份、统计和联动，不先清空 Canvas，也不产生空白中间帧。
- Sidecar status 暴露 `compute_profile=float32_batched`、计算镜像字节数、预热耗时和单帧各通道耗时；异宽指纹必须直接报错，不能静默丢行。
- OCR runtime 复用已通过场景门的三个卡名 ROI，在单 CPU 工作线程中运行 `ch_PP-OCRv4_rec_infer.onnx`。每张 RGB 名称图先以亮色阈值 130 求文字包围盒、四边扩展 5px 并限制在原图内；没有有效亮色内容时保留原图。裁剪结果同时进入模型、精确 SHA 与 cache，摘要域分隔为 `OCRMODELINPUTv2`，不得复用旧输入结果。`HEXTECH_OCR_MODE` 支持 `off/observe/admit`，两个 OCR 环境变量都未设置时默认 `admit`；旧 `HEXTECH_OCR_SHADOW_ENABLED=0/1` 仅兼容映射为 `off/observe`。模型按固定大小和 SHA-256 校验，初始化、校验或推理失败记录兼容诊断 `ocr_shadow_unavailable`，模板主链继续运行。production mailbox 按 `(session, epoch, slot, slot_generation)` 独立 coalesce，三槽 round-robin 并可合批；新帧只能替换同槽旧任务。`observe` 使用独立低优先级 latest-only 队列和 5 秒限频，production 始终优先。
- OCR 只与当前 production pool 的唯一数字 canonical ID 词表匹配。生产准入必须同时满足规范化 `match_rule=exact`、唯一 canonical ID、`confidence >= 0.95` 和完整上下文绑定；`unique_containment`、编辑距离与 fuzzy Top-3 永远只作 Shadow 诊断。OCR exact 是 `medium` 证据，首次 READY 和不同身份替换都需要 3/5 个独立 captured frame；strong 模板与 OCR exact 身份冲突时记录 `ocr_template_conflict` 并 fail closed。模板为 medium/none 时，OCR exact 才可作为 `ocr_exact_fallback`。
- OCR cache 的无损输入身份为规范化单行模型 tensor 的 SHA-256，并绑定固定模型与 production vocabulary；禁止用感知哈希缓存文字身份。cache 只复用推理结果，每个 captured frame 仍重新绑定 session、epoch、slot、slot generation、感知 fingerprint 和 frame ID，作为独立 3/5 时序证据。公共事件只暴露已确认 canonical identity、slot generation 和 acceptance rule；raw OCR、绑定、拒绝原因、template conflict 与 transition 进入私有 `_raw_slots`、Vision timeline v2、Sidecar status/session report 的有限诊断字段。

## 后台数据准备与显示硬门

- 首帧和恢复帧先应用绑定游戏 HWND 的有效物理客户区，再绘制、映射。Tk 的 1×1 占位尺寸和零/反向矩形不允许进入排版，不得因此推导 8px 字号。正常布局仍忽略逐帧识别 transform。
- 2026-09-06 已确认30px说明样图方向：2560宽固定30px，1920宽固定23px，720p保留兼容。安全区参考宽376/282px，左右至少6/5px留白。普通统计保留标准空格；仅超宽行按细空格、极细空格、去除排版空格依次适配，不缩字号、不压缩字形、不删字段，仍不适配则验收失败。Tk实际bbox与离线度量分开记录；所有新锚点标为`pending_real_device`，说明素材和旧图不能代替各规格真实内框校准。
- 四档分辨率/五档DPI参数化覆盖不等于逐规格实机确认；16:9锚点须以真实同规格、指引关闭的画面复验，不得用拉伸16:10图片宣称完成。字体度量对Canvas使用弱引用，避免字体缓存循环把Tcl清理推到后台线程。

- `OverlayDataPreparation` 是 Host 内唯一的数据准备工作线程，拥有 generation、Stage、scoped stats 与 hint 缓存；待处理请求容量为 1，按游戏实例、session、窗口、英雄、epoch/revision 和槽位身份合并。Tk 只读事件/上下文、消费准备结果、绘制与处理显隐，不执行整代哈希校验、全量 hint 复制或 scoped 文件加载。
- Host 启动即在后台校验当前 seed，尚无游戏局时仅记录 bootstrap generation；可信游戏实例出现后固定本局 generation，英雄确认后预热 scoped 视图。新局或请求版本变化会丢弃旧结果，退出时停止工作线程；冷启动暂时没有 snapshot 时按有限间隔重试，不能永久固定空视图。
- `DataSnapshotView.get_overlay_display_hints()` 只为 Host 提供名称、tier、联动与名称索引的独立副本，不复制未参与显示的全英雄历史统计；原 `get_overlay_hints()` 与不可变 generation 内容不变。Host 只缓存最近三代 display hint。
- 场景确认即绘制轻量 detecting shell，不等待完整数据投影。有匹配的逐槽联动可先于慢 scoped 统计发布；统计尚未确认时显示“统计准备中”，不伪造百分比。没有匹配明确记录 `NO_MATCH`，不解释为抓取延迟。
- 碎片 `body_shard` 同时经过 Tracker 和 Host 的硬门，清空普通卡缓存并作废在途数据结果；短暂按钮/卡面漏检、鼠标遮挡、Alt-Tab 和旧 OCR 均不能解除。明确选择完成、连续 0.75 秒无按钮/卡面/名称的结束确认或新游戏实例才结束该状态，Host 仍拒绝同轮或更旧的 READY 回流。
- 碎片 observation 与 terminal 同样进入有界 timeline v2，但始终不计入普通 Hextech 性能分母。默认不增加 PNG，也不改写历史 timeline。
- session report v2 可选增加 `render.layout`、`display_context`、`data_preparation`、`synergy_states`、`synergy_slots_drawn`。呈现中的 `bound_timing` 冻结同一次绘制的 event/host-read/context/draw 时间；后续轮询不得覆盖已绘制事件的阶段耗时。来源就绪、没有匹配、尚未绘制与实际绘制分别记录，报告仍不能代替人眼真机确认。

## 分来源 freshness 与联动

- generation 的聚合 `health=degraded` 只用于 Desktop 状态页和诊断。Overlay 的 ARAMKit Stage/`all` 行只读取 `source_status.aramkit`，回退 Blitz tier 时只读取 `source_status.blitz`；一方 fresh 不得掩盖另一方 stale。旧 `hextech/stats` generation 仅用于已固化回滚包，不参与新候选的阶段投影。
- ARAMKit 为 fresh 而 Blitz 只能复用同 Catalog 的 verified last-good 时，generation 仍以 `data_status=fresh` 提供阶段/全量百分比，聚合 `health=degraded`、`data_reason=optional_source_stale`；Blitz 状态必须为 `last_good/data_stale/production_coverage_insufficient`，Canvas、session report 的公开行和公开 DTO 均清空 tier/rank/score。只有 ARAMKit 本身 stale 或失败时聚合状态才是 `data_stale`。
- 已通过 scoped manifest/path/size/SHA-256 校验的 ARAMKit 记录即使 `last_good/data_stale` 仍可用于 lineage 和诊断，但 Overlay 必须 fail closed：清空胜率、出场率、排名和 tier，不得把过期数值伪装为当前统计；显示 `统计数据为 N 小时前`、`统计数据为 N 天前`，时间缺失/未来/不可解析时统一显示 `统计数据暂非最新`。原始 stats、sample count、generation、run、scope 和 `data_at` 只保留在 recommendation DTO 与 session report。只有来源明确 fresh 时才允许 Canvas 绘制百分比。
- `DataSnapshotView.status(now=None)` 每次读取都按共享的 `SOURCE_INTERVALS × 1.25` 策略重新投影各来源绝对时效，优先使用 `data_at`，旧 generation 缺失时回退 immutable manifest 的 `created_at`；ARAMKit 的实际边界为 5 小时。超过边界只设置 `data_status=data_stale`、缺少更具体原因时设置 `data_reason=source_data_expired`，并记录总年龄 `stale_age_seconds`；不改写 manifest、`health`、`degraded_sources` 或表示 lineage 的 `freshness`。推荐、阶段统计、session report、桌面状态和 Overlay hint cache 都消费同一实时投影；桌面“数据 X 前”优先使用 ARAMKit `data_at`。Apex/Mayhem 的来源状态仍只影响联动区域，不污染胜率和出场率。
- DataService 的构建顺序固定为“ARAMKit scoped artifact → Blitz 海克斯 tier 排名投影 → Catalog 补全名称、稀有度 tier、图标和最终身份集 → 当前 generation 联动投影”。Host 在 game session 首次出现时固定 `stats_generation_id`，同局后台换代只记录 `new_stats_generation_id`，下一局才采用；stats-only 换代不重建 Vision matrix。Host 从同一 Live Client 请求读取 `championName + level`，按 3–6/7–10/11–14/15+ 解析 Stage 1–4；等级缺失时仅使用本局已确认的 0–3 次选择推导下一 Stage。每个 epoch 最多等待两秒并固定 champion、Stage、ARAMKit run 和单英雄 view，下一 epoch 可重算阶段但继续使用本局 generation。单英雄文件按 index path/size/SHA-256 校验，LRU 容量为 2。阶段只在游戏窗口右上角显示单行 `阶段 N`，卡片不拼接阶段、样本、综合回退或 freshness 文案。`sample_count < 100` 时胜率仍显示、出场率改为真实 `出场数 N`；`100–999` 保留两个百分比；两档当前阶段统计用柔和红 `#F87171`。`sample_count >= 1000` 的当前阶段统计沿用金色。`stats_scope=all` 的综合回退始终用蓝色 `#3FA9DC`，低于 1000 时再叠加红色细内框；缺少或非法 `sample_count` 时保留百分比并不推断低样本。Blitz 的 `source_tier` 是全局排名，`champion_tier` 只在来源列出的最多五个英雄中存在，当前用“该英雄 Tn · 全局 Tn”或“全局 Tn”展示。
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

Host 呈现必须按 `should_show → canvas_drawn → map_requested → mapped → composed/failed`
解释。`game_overlay_visibility.v1.json` 继续保持 schema v2；兼容字段
`decision.window_visible` 只等同旧版显示请求，和 `decision.should_show` 同义，绝不代表
用户实际看见像素。Host 创建顶层 HWND 后、首次映射前必须设置并回读
`WDA_EXCLUDEFROMCAPTURE`；visibility/session report v2 在 `presentation.capture_exclusion`
写入可选的 `status=applied|unsupported|failed`、requested/applied affinity、query/set 结果和有限原因码。无法确认 `applied` 时立即 withdraw 并以 `capture_exclusion_unavailable` fail closed。
`presentation.state=mapped` 只证明 HWND 的 `WS_VISIBLE`、非 iconic、非 cloaked、扩展样式和同 DPI 物理 client rect 通过；`host_surface_probe=matched` 证明当前 Canvas 存在足够的不透明内容，affinity 回读后 `composition_probe=excluded` 才进入 `composed` 并写 `presented_at`。Desktop DC/GetPixel 在不同 Windows 构建上可能返回人眼合成像素或黑色，不能再用“是否看见 Overlay 颜色”判断捕获泄漏。packaged presentation smoke 另用已知底色、高对比探针和 Sidecar 同类 `ImageGrab` 路径证明 `desktop_capture_excluded`；Full Screen/unknown 下仍不得运行该捕获门。默认运行不保存截图，真实 League 最终可见性仍须用户确认。

Host 使用单线程有界队列异步写入报告，Tk 渲染线程不执行 JSON 文件写入、历史轮转或截图。队列满时合并同状态任务并优先保留最新结果；`game_overlay_visibility.v1.json` 暴露 `report_queue_depth` 和 `report_dropped_count`。完整 READY revision 首次达到 `composed` 后立即提交 JSON evidence；只有显式截图仍要求两个稳定 presentation tick。

- 会话报告：`var/reports/overlay_sessions/`
- 最新报告：`var/reports/overlay_sessions/latest.json`
- 历史：最多保留 200 个 `overlay-session-*.json`（一局约 58 份，须容得下至少 3 局完整证据）
- 真实会话证据：`var/state/session_evidence/`
- 逐选择观察：`var/state/overlay_vision_timelines/selection-*.jsonl`
- 时间线轮转：只在 schema v2 集合内最多保留最近 20 个真实 selection epoch；每文件最多 1 MiB/512 条，并为一次终止 marker 预留空间。v1、空文件、损坏或身份不明 timeline 永久 pinned，不占 v2 数量上限和 12 MiB 分类预算
- 默认模式：不生成 PNG
- 显式 `--diagnostic`：允许后台线程异步保存 Overlay 矩形裁剪图，不保存完整屏幕；每个合格 active Hextech epoch 最多保存第一张完整三槽 READY 裁剪图，并沿用 session-evidence 留存

`evidence_starved` 会进入有界失败证据链：Runner 从 Tracker 内部显式传递三槽 `slot_generation`，该私有字段不进入 Overlay event。每个 `(session, epoch, slot, slot_generation)` 只提交一次最佳名称、图标和选择按钮 ROI，不保存完整游戏画面。记录包含 production pool、候选、confidence、margin、通道分歧、场景类型和失败原因；内容寻址图片跨会话去重，writer 队列上限 32、唯一记录上限 200、每记录只保留最新 20 次 occurrence 并另记累计次数。淘汰记录时同时清理 index 和无引用 blob，写入失败只进入有界 journal。所有记录固定 `automatic_exemplar_eligible=false`、`requires_manual_truth=true`；只有人工绑定并通过独立 corpus replay 后才能成为生产资源。

五类 ROI corpus 为普通海克斯、锻体碎片、动画、淡出和无效裁切。私有真机 ROI 与人工标签冻结在本 worktree ignored `var/recognition/corpus`，不得提交 Git；`tooling.diagnostics.overlay_roi_corpus` 校验允许根、文件大小、SHA-256、碎片 hard block 和非普通场景 `ready_slots=0`。2026-08-14 当前普通、动画、淡出和无效裁切已有冻结 private runtime report；旧私有碎片 ROI 已被 live debug 轮转，只剩仓库内真实脱敏 fixture 可验证像素 hard block，因此本轮 `body_shard` private runtime report 门仍为未完成，必须由后续至少两次真实碎片选择补齐，不能以 fixture 代替。

报告 v2 记录捕获、识别完成、事件写入、Host 读取、上下文确认、绘制开始/完成、HWND 映射、合成探针、报告入队和落盘时间；`draw_completed_at` 只表示 Canvas 命令完成，`mapped_at` 表示窗口事实通过，`presented_at` 只有受支持窗口模式下 `probe_contract=dwm_desktop_dc` 的像素探针 `matched` 时存在。报告复制 Host 的 `game_window_mode`，并在 `source.dpi_scale` 与 `render.typography` 保存实际字体度量；两者也进入去重签名。`source` 同时记录 selection epoch/revision、scene 与 window active。槽位记录 `vision_id`、有限候选、`canonical_id` 和最终缺失原因；vision 槽、render 行与 context 在写入时合并。排查延迟时按时间链路定位，不用截图文件时间代替阶段证据。

Vision 时间线 schema v2 每个 observation 记录独立序号、`build_id`、Sidecar PID/instance、三项捕获/识别时间、会话/游戏实例、`selection_type`、`selection_window_active`、`game_window_mode`、三槽识别证据、公开状态、revision、mouse event sequence、transition source/slot、逐槽 generation 与 generation-change reason；文件名同时隔离 Build 与 Sidecar instance，单文件不得混入其他身份。`transient_pause` 与携带原 epoch/revision 的 `gameflow_ended` terminal marker 也必须写入同一 epoch 文件；未截图事件标记 `capture_status=not_captured`，空闲心跳仍不写入。append 实际成功后 writer 才能更新 `current_timeline_path_hash`、`timeline_last_written_at` 和 `timeline_epoch`；失败或截断必须保留准确 stage/error/counter。报告生成晚于事件一秒仍找不到对应 v2 时写 `diagnostic_contract_error=timeline_missing`；`FileNotFoundError`、写入失败或 terminal 缺失都是自动门失败，不能降为警告。

Sidecar 在识别完成后先原子发布用户可见 Overlay event，再把 trace/timeline 交给容量 64 的单线程诊断队列；队列满或磁盘写入失败只增加 Sidecar status 的 `diagnostic_writer` 丢弃/失败计数，不阻塞下一帧，也不改变候选、READY、revision 或 Overlay event。writer 在 deepcopy 前拒绝空 session/game、无效 epoch 和重复 idle probe；captured recognition 正常写入，visibility probe 只在签名变化或 30 秒心跳时写入，`selection_completed/scene_loss_confirmed/gameflow_ended` 每 epoch 只写一次。Host 无游戏窗口、游戏存在但未进入选择、选择期/1.2 秒 fast hold 分别以 250ms、50ms、16ms 读取本地事件；场景确认后即显示阶段与三槽 detecting shell，不等待首个 READY。Canvas 绘制后经 8ms 延迟启动有限像素合成检查；session report 用 `presented_event_written_at`、事件 epoch/revision 和 `ready_frame` 把合成时间绑定到实际被绘制事件，禁止与 composition 回调时读到的更新事件误配。

持续诊断统一登记在 `diagnostic_retention.v1.json`，全局硬预算为 128 MiB，不允许搬到 Temp、release、桌面或其他目录规避。分类上限固定为：session evidence 48 MiB/30 天/100 bundles，`debug/overlay_vision` 28 MiB/7 天/32 observations，failure-inbox 12 MiB/30 天/200 records，Vision timeline v2 12 MiB/14 天/20 epochs，Overlay session reports 8 MiB/14 天/200 reports，runtime logs 12 MiB/14 天，Supervisor events 4 MiB/14 天（1 MiB active 加 3 段），trace/status 4 MiB/14 天。`diagnostic_retention` 是所有诊断删除/轮转的唯一所有者；writer 只 append，Host、Sidecar、Desktop 都不得自行扫描删除。留存器使用共享 `var/locks/diagnostic-retention.lock` 跨进程独占锁和共享最短运行间隔 60 秒，selection active 时不启动全目录清理；60 秒内请求记录 `skipped_interval`，锁忙记录 `skipped_lock_busy`，只有测试可显式 `force=True`。`diagnostic_retention.v1.json` 的 `last_run.disposition=completed|skipped_interval|skipped_lock_busy`、`next_eligible_at` 和锁结果必须可审计。只有首条非空 JSON 明确为 schema v2 的 timeline 才在 v2 集合内部执行年龄、数量、单文件和分类字节门；旧 v1、空文件、损坏或身份不明 timeline 永久 pin 为只读，且不占 v2 数量或分类字节。删除前验证目标仍位于登记根并拒绝 symlink、junction、reparse point 或越界路径。无法安全清理时丢弃新的低优先级诊断并累计错误，不触碰 generation、source run、Catalog、asset、模型、模板、设置或用户数据。

性能报告只把同一目标 Build、同一 Sidecar instance 且同时满足 `selection_type=hextech`、至少一帧 `scene_state=active`、至少一帧 `selection_window_active=true` 的 epoch 计为合格 Hextech epoch。`candidate`、`body_shard`、`blocked`、纯 pause、`gameflow_ended` 和不足一次 active 的临时 epoch 必须从三槽覆盖率、首次 Canvas 与 recognition P95 中排除，并分别写入 `excluded_epochs_by_reason`。报告同时输出 capture、recognition、capture+recognition total 三段，保留每 session 最慢 10 个 observation 的纯结构化 `matching_timing`，不增加图片。不得通过排除慢帧、修改时间戳语义或减少真实样本伪造通过。

`overlay_vision_trace_history.v1.json` 仅在状态签名变化时追加；`cursor_over_cards` / `hover_occluded` 属于鼠标位置噪声，不参与签名（字段仍写入条目），避免空闲期把 256 条历史冲成无价值的空槽帧。离线分离度分析使用 `tooling/diagnostics/vision_separation.py`：读取 `overlay_vision_timelines/*.jsonl` 与人工真值，输出三通道 top1 命中率、命中/未命中的 confidence 与 margin 分布及 Cohen's d；识别阈值只允许依据该报告调整，不得盲调。

显式设置 `HEXTECH_OVERLAY_SIDECAR_DEBUG_DUMP=1` 可启用 diagnostic 模式：为每个真实 selection epoch 连续保存前 5 个独立 observation 的按钮、三槽图标和三槽卡名 ROI，每组带 observation 序号、三项时间戳和固定有限的 `matching_timing`；不保存完整游戏截图，并沿用受限轮转。识别线程只把帧引用和事件快照提交给容量 8 的 `roi_dump_writer`，裁剪、PNG 编码、JSON 和目录轮转全部在后台完成；满载或失败只增加 Sidecar status v2 的可选 `roi_dump_writer` 计数，不改变候选、READY 或 revision。ROI 位于 `var/debug/overlay_vision/overlay_roi_v2`。持久开关位于 `var/state/overlay_diagnostic_settings.v1.json`，解析优先级固定为 Sidecar 启动显式参数、显式环境变量（含关闭值）、持久设置、默认关闭；部署器的 `preserve/on/off` 只在显式部署时生效，失败必须恢复部署前原始设置。Sidecar status v2 的 `debug_dump_enabled` 是部署验收字段，不改变协议版本。识别改动前至少收集 3 局、10 个带人工真值的有效 epoch，并用 `tooling/diagnostics/vision_separation.py` 分析；单个截图不得复制成多帧证据。只有独立构建样本与 holdout 均满足 observed-name `confidence >= 0.92`、`margin >= 0.08`，且既有 full-frame fixture 无 false-ready 时，才允许增加脱敏 `name_exemplar`。旧 Build 真机报告测得端到端中位数约 207.5 ms、P95 约 259.2 ms；本轮只建立真实指标，若新 Build P95 仍高于 180 ms，应单独优化热路径，不能通过放宽识别规则掩盖。

## 对局期间的数据刷新

存在可用 current generation 时，对局期间延后 Catalog、ARAMKit、Blitz、Apex、Mayhem 的自动刷新和手动核心刷新，状态为 `refresh_state=deferred`、`deferred_reason=game_in_progress`。这不是 `data_stale`，不计入失败和 backoff。Desktop 标题栏“刷新”调用 `POST /v1/actions/refresh` 的 `scope=core, force=true`；空 body 仍是旧的 due check。核心强刷不会无条件抓取 Optional，但正常到期的 Apex/Mayhem仍同行，ARAMKit marker 或 Catalog 变化会扩展为完整同代刷新。

刷新门由 DataService 的独立三态探针提供，不以 Host visibility 作为唯一事实源：Live Client 2999 或 LCU gameflow 明确 `InProgress` 时在局中；接口 unknown 但 `League of Legends.exe` 或游戏窗口存在时保守按在局中；只有无游戏进程/窗口且 LCU 明确非对局或不可用时才允许刷新。worker 运行期间每不超过 50 ms 读取 cancel signal；游戏门、Desktop shutdown 和 hard timeout 分别写 `cancel_reason=game_in_progress/shutdown_requested/hard_timeout`。取消先给 2 秒协作退出窗口，再关闭 Job Object 回收完整进程树；游戏取消不写来源失败/backoff，shutdown 不发布新 candidate 或 generation。

当前正式活动 Catalog 独立刷新：ARAMKit fresh 后可以发布新 generation；Blitz 失败时只能复用同 Catalog 且哈希验证通过的 last-good 并 fail-closed 隐藏排名，Apex/Mayhem 必须成对复用同 Catalog last-good。ARAMKit 失败不发布新 generation。Catalog 内容 SHA 变化时禁止两阶段混代，所有依赖来源必须绑定新 Catalog 后才能整体晋升。

若 fresh ARAMKit 上游出现活动 Catalog 未登记 ID，默认仍整轮拒绝；唯一例外是该 ID 同时存在于已验证的 blocked adoption Catalog。此时只把这些 adoption-only 行从活动 artifact 的 `all/stages` 投影中剥离，并在 ARAMKit manifest/report 的 `compatibility_filtered_augment_ids` 记录精确 ID；任意未被 adoption 证明的未知英雄/海克斯、重复、非法 rate、空 stage 或错绑仍 fail closed。严格全链允许活动 Catalog 显示为 `adoption_held`，但 ARAMKit 仍必须在 4 小时内 `fresh/fresh`，generation 的 degraded 来源只能属于 Blitz/Apex/Mayhem 且必须 last-good/data-stale。

Blitz 采用同一窄兼容合同：只有已验证 blocked adoption Catalog 能证明、但活动 Catalog 尚未收录的海克斯 ID 才从排名 artifact 过滤；过滤后必须重算 artifact 的 `record_count` 与 canonical content SHA-256，并在 manifest/report 精确记录 `compatibility_filtered_augment_ids`。这不会切换活动 Catalog、恢复 adoption lane 或扩大 production pool；任何未被证明的未知海克斯、未知英雄、schema 错误和过滤后 production 覆盖不足仍拒绝整个候选。

`refresh_checkpoint.v1.json` 只记录当前活动 Catalog 的发布周期；`catalog_adoption_checkpoint.v1.json` 独立保存待采用的新 Catalog、已完成 candidates、真实 pending、失败原因与 backoff。首次读到 Catalog 不同且没有 core generation 的旧 `full_catalog_rebind` 时，原证据原子复制到 adoption checkpoint 并标记 blocked，原 checkpoint 标记 migrated 而不删除；`pending_sources` 始终等于 due 减 completed。活动 checkpoint 只为仍 pending 的来源保留 `reason_code/failure_stage/error_type/fallback_used/last_good_available/diagnostics`，完成来源的失败证据必须消失；诊断有深度、数量和长度上限，并剔除 traceback、命令行、环境、proxy、credential、token、cookie 与 secret。新 Catalog 只有 Blitz 覆盖至少 95% 且 ARAMKit、Apex、Mayhem 全部同 Catalog 后才能整体采用，普通 active refresh 不会续跑或晋升 adoption lane。

若刷新过程中进入游戏，Core 发布前取消 worker 并保留 checkpoint；Core 已发布后只取消或延后 Optional，已发布 generation 不回滚。checkpoint 与延后门保留原请求的 `scope/force`，对局结束后等待 30 秒只恢复一次等价请求。`GET /v1/status.refresh_status` 暴露 `state/scope/phase/reason_code/generation_id/pending_sources/started_at/completed_at`；Desktop 对 running/deferred 使用粘性文案，对 completed/unchanged/failed 显示 6 秒。没有可用 snapshot 的冷启动不受此策略阻塞。

Blitz marker 相同时也必须服从共享绝对时效：verified pointer 的 `last_success_at` 年龄在 2 小时 30 分以内才可返回 `not_stale`，恰好边界允许复用；超过边界、时间缺失或非法时完整抓取。所有来源与严格 verifier 共用 `SOURCE_INTERVALS × 1.25`，不再重复定义 Apex/Mayhem 阈值。Snapshot manifest 的 `health/degraded_sources` 仍是 immutable lineage；消费者数据读取时过期合并进 `effective_degraded_sources`，Catalog 由 `adoption_held` 独立门处理，Desktop 与严格门优先消费 effective 字段。

Blitz 的公开 JSON 仍由 Scrapling 静态 `get` 主抓；只有最终归类为 `tls_error` 或 `network_error`、host circuit 未打开且总预算仍有剩余时，才允许一次 `requests` 静态 fallback。403、429、schema/identity/coverage 错误、无效 payload 与 2 MiB 超限都不得 fallback；主路径和 fallback 都执行相同大小门与 TLS 校验。manifest/report 必须记录实际 `fetch_backend`、`fallback_used` 和 `fallback_from`，但不得引入 browser、stealth、代理、Firecrawl 或旧 `run/crawler`。

冻结包若已安装完整 verified bundle seed，DataService 必须先发布该 generation，再给首次自动远端刷新 30 秒启动宽限；手动刷新不受宽限限制，宽限到期若已进入游戏仍走既有 `game_in_progress` 延后/取消。Blitz 失败按 optional stale 保留同 Catalog last-good；ARAMKit 失败保留既有 current 供降级显示，但不发布 generation，也不能满足新候选 fresh-data 构建门。

## 备战席显示与客户端右侧停靠

本节的具体行为唯一以 [desktop-stable28.md](desktop-stable28.md) 为准；旧自由拖动和邻屏停靠不是当前功能。

- 桌面“备战席”与游戏内三槽Overlay是两个独立窗口。默认 `client_foreground`：大厅、房间、选人等客户端前台阶段均允许完整外壳，不等待LCU连接、候选列表或统计；客户端失前台、最小化、不可见、实际游戏或用户关闭时隐藏。面板自身前台不能授权显示，不在运行中切换策略或新增界面开关。
- 点击“×”保持手动隐藏，只有托盘“显示Hextech”或同Build快捷方式激活才能解除；下一次轮询、选人和数据刷新不得撤销。手动打开不绕过同屏右侧空间、实际对局、客户端前台可见性或新鲜状态硬门。
- 手动恢复必须等待请求之后的新鲜客户端窗口观察；不要求选人阶段。桌面探针把最小化游戏窗口也作为隐藏硬门，游戏窗口或有效局中证明存在时不得显示；客户端数据unknown不等同实际游戏不存在。捕获/识别调用仍只接受可渲染游戏窗口。
- 桌面以客户端显示器的 D_client=DPI/96 计算 S=clamp(客户区物理宽/D_client/1280,0.8,1.25)，期望宽320×S、高度上限740×S，再统一乘停靠目标显示器的 D_target 一次。右侧可用宽度不足时先缩窄重排，最低200逻辑像素；真实控件测得的操作区、状态区、一行英雄和间距决定最小高度。宽/高不足分别为`right_width_insufficient`/`right_height_insufficient`，不向左或客户端内部挤压，不自动移动客户端。必要文字最低12逻辑像素，跨屏DPI不沿用主屏或启动值。
- 不接受独立手动位置，不跳邻屏。前台客户端调整位置时，只有仍合法的已映射位置可用于三秒宽限；重复状态不续期，超时仍放不下就隐藏；相交部分裁除以免遮挡客户端。
- 同屏右侧不足的具体原因写入现有状态和托盘，宽度不足提示“请将客户端左移以留出空间”。不自动移动客户端、不创建空白窗或持续通知。暂时拓扑查询失败保留最近完整拓扑，真实断屏才更新布局。
- 首次及恢复映射前应用物理布局，未变化状态不重复deiconify/lift；持续NOACTIVATE保留点击但不抢走客户端/游戏焦点。新策略仍需混合DPI真机交互验收，纯布局/原生Tk测试不替代该门。
- `DesktopWindowPresentation`是唯一Tk窗口呈现owner：后台窗口观察与选人阶段进入容量一mailbox，25ms GUI tick应用最新结果；失效句柄和陈旧观察不得复用旧定位。直接销毁根窗口也必须停止观察线程、取消回调并断开UI/owner反向引用，不能让Tcl对象被后台GC回收。
- 复用已有本地LCU上下文，单在途请求在客户端前台约250ms、后台1.5s轮询。阶段仅决定内容，慢列表不阻塞窗口显隐；gameflow HTTP在线程中缓存，窗口探测不等待HTTP。短暂断连沿用ClientContext有界内容保留，不携带上一局英雄进入新局，不再用degraded内容状态阻塞客户端前台外壳。
- 前台切换时等待周期在50ms内重算，不能沿用后台1.5s睡眠。候选列表使用独立串行线程和容量一mailbox，Web预热的同步检查不再进入阶段轮询；迟到的列表回调只在候选仍为最新且未关闭时应用。
- 启动先创建隐藏窗口/托盘和轻量观察器，再准备重服务。`ui_ready`只表示Tk控制面可用，`first_idle_visible`只在真实映射后记录，不为启动服务先展示空白长窗。
- `desktop_window`结构化日志仅在状态变化时写入策略、阶段、隐藏原因、用户隐藏、目标/实际矩形、阶段确认和映射时间；托盘暴露简短隐藏原因。沿用runtime日志留存，不增加默认截图或协议版本。
- 双屏至少10次独立选人进入验证：合格场景phase→mapped P95≤100ms、单次≤300ms，用户可见进入选人→展开≤500ms；漏显、需移动客户端才出现、闪烁均为零。不通过则按明确选定的`client_right`候选重新验收，关闭保持、不遮挡和对局隐藏不能降级。

## 系统托盘、轻量待机与无终端启动

- Desktop 右上角“×”和 `WM_DELETE_WINDOW` 只隐藏到 Windows 系统托盘，不停止识别或退出进程。完全退出只能使用托盘菜单“退出 Hextech”。托盘还提供“显示 Hextech”“重启识别”和只读运行状态。
- Desktop 单实例 owner 使用 build-aware v2，记录 `build_id`、`source_fingerprint`、规范化 launch/真实 executable、PID、真实 process create time、owner ID 与启动时间；存活验证同时核对 PID/create time/executable，避免 PID 复用。只有同 Build 才能写 `desktop_ui_activation.v1.json` 并激活；不同 Build、身份缺失、旧 v1 owner 或新 owner 收到 v1 activation 时写有限 conflict state 并显示当前/请求 Build 与当前 EXE，不静默激活、不终止任何进程。
- Overlay Host 与 Vision Sidecar 分别持有共享 `var/locks` 下的 OS 独占文件锁；第二实例必须在创建 Tk 窗口或写 bootstrap、status、事件状态前退出，进程异常结束时锁由操作系统自动释放。
- 连续 300 秒既无 `LeagueClient.exe` / `LeagueClientUx.exe`，也无 `League of Legends.exe` 时进入轻量待机。客户端即使最小化，只要进程仍在就不待机；单独的 Riot Client launcher 不算 League 活动。
- 轻量待机停止 DataService、Web、Runtime Supervisor、Overlay host 和 Vision Sidecar，只保留 Desktop、托盘与 League 进程探针；探针按运行态分档——待机/恢复失败态 1 秒（保 15 秒唤醒预算），服务运行期 5 秒（仅用于 300 秒空闲判定，避免对局与大厅期间每秒全量枚举进程）。真正停止前必须二次探测 League，避免探测后到停机前的竞态；League 客户端或游戏进程出现、托盘显示、托盘重启识别或快捷方式激活会恢复。自动恢复的端到端预算为 13 秒，连同最坏一轮探测仍小于 15 秒；恢复逐项验证 DataService、Supervisor、Host、Sidecar 心跳和 Build ID，失败后 League 仍存在时每 5 秒重试，托盘“退出 Hextech”后则不再自动启动。
- `var/state/background_runtime_transitions.v1.json` 是独立 schema v1 的有界生命周期诊断，最多保留 200 条状态转换，仅记录原因、匹配进程名、组件结果和错误类型，不记录命令行或敏感信息。
- 识别运行态必须区分 `suspending`、`suspended`、`resuming`、`restart_in_progress`、`resume_failed`、`resume_cleanup_pending` 与真实 Sidecar `stale/failed`。主动待机文案为“识别已休眠”，不得显示为“识别失效”；`resume_cleanup_pending` 表示先回收上次失败的残留服务，回收未确认时不得并行启动新 Supervisor。Supervisor 发现已启用 Sidecar 存活陈旧时先发布 `starting / sidecar_restart` 并显示“识别重启中”；只有既有恢复预算耗尽后才进入最终错误态。
- PyInstaller 主程序必须使用 Windows GUI subsystem（`--windowed`）。Supervisor 与 DataService 使用带随机 token 的原子 bootstrap 文件握手；所有本程序 Python 子进程同时使用 `CREATE_NO_WINDOW`，不得依赖 `sys.stdout` 或弹出控制台。桌面快捷方式和 packaged smoke 都直接启动 EXE；便携 BAT 仅为兼容入口，可能短暂闪烁。
- 冻结入口必须先识别 `--data-service`、`--runtime-supervisor`、`--game-overlay`、`--overlay-sidecar`、`--acquisition-worker` 等角色；子角色直接进入各自入口，只有 Desktop 在首屏和 runtime logging 就绪后作为唯一 owner 执行一次 cohort seed。DataService/Supervisor 在 `Popen` 成功后立即绑定 Windows Job Object 并登记到 Desktop pending-process registry，不能等待 bootstrap JSON；冻结态绑定失败即启动失败。每个阻塞阶段前后检查 Desktop cancellation，退出、bootstrap 失败或超时时先关闭 Job 再清理 bootstrap 文件，尚未发布 PID 的进程树也必须回收。
- Desktop 启动服务前只根据受管状态中的 PID、process create time、真实 executable 与 Build ID 检查残留 DataService、Supervisor、Host、Sidecar；活着的跨 Build 或 orphan role 精确报告 PID/Build/路径并阻止启动。不读取命令行或 LCU token，不按进程名批量终止，不自动杀旧包。
- Overlay Host readiness 在源码入口保持 5 秒，在 PyInstaller 冻结包为 20 秒；进程提前退出或 token 不匹配立即失败。readiness 失败后必须确认 Host 已退出，清理失败时不得继续启动 Sidecar。Supervisor 事件与状态快照记录 Host 启动耗时、ready 状态、尝试结果和 PID。

## 打包、部署与旧包防错

打包前运行目标测试、完整 pytest、开发门禁、Ruff、Pyright、Scrapling 静态 smoke 和 packaged startup smoke。普通打包只生成候选，必须明确标记“候选未部署”；构建只允许使用 manifest v3。部署器拒绝缺少构建身份或 `runtime_contracts` 不等于 Overlay v3、Sidecar v2、session report v2 的候选。

使用 `--verified-snapshot-root` 构建时，bundle 除 snapshot seed 外还必须携带 `resources/cohort-seed`：当前 Catalog generation（含 content-addressed 图标和 `augment_assets.v1.json`）、ARAMKit/Blitz/Apex/Mayhem immutable run、六个 current 所需绑定与同代 schedule。冻结 Desktop 在 runtime logging 与首屏就绪后、启动 DataService/Supervisor 前安装该 seed，复用相同哈希文件，最后提交 snapshot pointer；所有子角色禁止 seed。不删除旧 generation 或历史用户数据。`--refresh-data` 成功时必须以实际刷新后的 `var/snapshots` 为打包输入，不能继续打旧 `resources/seeds`。

打包器在任何 packaged smoke 之前枚举冻结目录中的 `*.dll` 与 `*.pyd`，按候选落盘后的真实绝对路径执行 Win32 原生加载预算门；任一路径超过 259 字符就以 `native_runtime_path_too_long` 拒绝候选。构建 artifacts 根和 release 名必须保持短，例如本工作树使用 `.artifacts\hx`，不能把长 `.tmp_overlay_*` 根继续叠加进 PyInstaller `_internal`。每个 packaged fixture 在启动 Desktop 前还必须运行同一 EXE 的 `--acquisition-worker --self-check`，验证 `curl_cffi._wrapper`、ARAMKit service 与 Blitz service 都能从冻结路径导入并绑定当前 Build ID；该自检无网络，失败时不得继续 smoke。

bundle 安装前先恢复未完成 promotion journal，再枚举 current、previous、`cohort_recovery_point.v1.json`、全部本地 immutable generation 和 bundle generation。每个候选都必须重建并验证 generation、provenance、Catalog、四来源 run/manifest/artifact/hash 与 production pool 绑定；只在完整候选中按 generation `created_at` 单调选择最新者，来源优先级只用于同时间平局。旧程序把 current 倒退为 G1、previous 仍指向 G3，而新 bundle 携带 G2 时必须恢复 G3，并记录 `install_state=runtime_restored`；`cohort_selection.v1.json` 保存选择来源、选中代、bundle 代和全部 valid/rejected 诊断。schedule 与 recovery point 和 generation pointer 由同一 promotion journal 提交，崩溃必须整体回滚；retention 必须保护 recovery point 直接引用及其 provenance 闭包。

完整验证成功后写入 `cohort_validation_receipt.v1.json`。只有 Build/source fingerprint、current pointer、generation manifest、全部验证文件的 size/mtime 与 validator contract 完全一致且 promotion journal 不存在时，Desktop 才可走 metadata 快路径；任一漂移立即回退完整哈希。真正 promotion 重新写 receipt，写失败只影响下次启动。刷新候选的 Catalog、四来源 immutable pointer/provenance、payload 与 production pool 内容身份完全不变时，返回 `promotion_disposition=unchanged`，不创建 generation、不移动 previous、不打开 promotion journal；`checked_at`、`stale_age_seconds` 和本轮 refreshed sources 不参与 immutable generation 身份。

Sidecar 另发布 `vision_pool_fingerprint`、`vision_pool_origin_generation_id` 与 `observed_data_generation_id`。fingerprint 只由 Catalog/pool、模板资源和矩阵合同决定；stats-only promotion 不重启 Sidecar，Host 下一 game session 才采用新 stats generation。fingerprint 真变化时 Supervisor 先预热，selection active 时延后，inactive 后以显式目标 generation/fingerprint 切换；失败只恢复旧 Sidecar，Host 保持运行。旧 Sidecar 心跳恢复记录 `sidecar_recovered`，只有 PID/instance 与 readiness 真变化才能记录 restart。

packaged startup smoke 必须以 Web 关闭、Overlay 与私用统计开启的配置，串行验证 clean、stale-sidecar 与 populated-runtime 三组 fixture；populated-runtime 预装 8 个 generation、其中 4 个 legacy，并携带 current/previous/recovery 与 blocked Catalog adoption。三组都必须真实拉起 Desktop → DataService → Supervisor → Host → Sidecar，在既有 20 秒 Host 预算内 ready，且只出现 Desktop seed owner；同时核对当前 Build、存活 PID、连续心跳、Host `stats_generation_id`、Sidecar/event `vision_pool_generation_id`、角色声明与带结构化 `stats_scope` 的当前 Build session report。每个 fixture 还必须创建已知底色的模拟游戏窗口，绘制三槽 READY 与三行统计，运行生产 Tk/Win32 presentation 状态机，并要求 HWND、物理 rect、扩展样式、非 cloaked 与有限像素合成探针全部通过。冻结 Host self-check 与 Sidecar `--once` 继续保留，`--once` 仍核对 Vision pool generation、Catalog、pool ID、production pool count、`rank_identity_count`、各矩阵行以及 `observed_name > 0`，但它们不能单独令 smoke 通过。

v5 packaged smoke 还必须预置 20 个真实形态 v1 timeline，再写入 v2 并至少执行两次留存，证明 v2 与 terminal marker 仍存在；同时覆盖 Full Screen blocked 与 Borderless resumed。smoke 只能判定自动门，不能宣称真实 League GO。

参与 Build 一致性核对的 runtime state（`startup_timing.v1.json`、sidecar status、visibility、session report 与 Vision timeline v2）携带 `build_id` 以自证来源；`background_runtime_transitions.v1.json` 与 trace history 仍是纯诊断。部署到 `C:\HextechCompanion` 后，真机测试前必须核对：

1. `Hextech伴生终端.exe` 的修改时间、FileVersion 和 SHA-256 属于本次构建。
2. `_internal\bundle_manifest.json` 的 `build_id`、源码 revision、源码指纹和契约版本完整。
3. Desktop 状态、`game_overlay_sidecar_status.json`、`game_overlay_slots.v1.json`、`game_overlay_visibility.v1.json`、`reports/overlay_sessions/latest.json` 的 `build_id` 与 manifest 一致。
4. Sidecar status 为 v2、Overlay event 为 v3、session report 为 v2。旧状态文件只能作为历史证据，不能证明新进程已启动。

部署过程不得清理 `%LOCALAPPDATA%\HextechNexus\var`、历史报告或用户数据。显式部署获准后，部署器不依赖托盘退出或窗口 `WM_CLOSE`：候选复制与 hash 校验完成后，强制结束所有名为 `Hextech伴生终端.exe` 的稳定版、`.previous`、便携版进程，以及源码启动的 Overlay Host / Vision Sidecar；任何残留或权限不足都必须中止目录切换。已有更新 runtime 的 current validation receipt 只能跳过当前代重选，不能跳过新 Build bundle baseline 的逐文件校验、immutable 物化和完整 cohort 验证；baseline 物化不得改写更新的 current pointer。

新版本落盘后无论旧客户端此前是否运行，都必须直接从 `C:\HextechCompanion\Hextech伴生终端.exe` 启动。部署器只有在以下条件同时成立时才返回成功：Desktop、DataService、Supervisor、Overlay Host、Vision Sidecar 均来自稳定目录且各 1 个；自动刷新期间允许存在同一稳定目录的短生命周期 acquisition worker，但它不算常驻角色；不存在旧目录或源码识别进程；Catalog、ARAMKit、Blitz、Apex、Mayhem 与 snapshot 六个 current 属于同一完整 cohort，schedule、checkpoint、recovery point 与 current generation 收敛且来源没有 failure。若启动期间恰好完成到期刷新，只允许接受经完整 generation/provenance/source artifact/hash 验证、时间不早于 bundle seed、且 Catalog 与 production pool 身份完全不变的新 generation；Sidecar 的 Vision generation 可继续使用 bundle seed，但 Host/session report 的 stats generation 必须跟随新 current，不能把跨角色合法分离误判成混合 Build。Sidecar status 必须为 `running`、PID 对应唯一 Sidecar、production pool 与矩阵身份数精确匹配；`startup_timing.v1.json`、Sidecar status、Overlay event、visibility 和最新 session report 均由本次启动刷新，协议版本正确且 `build_id` 与 bundle manifest 一致。任一条件超时或不一致都触发部署失败并恢复安装目录、ROI 设置和部署前 13 个 cohort pointer、schedule、checkpoint、recovery、selection、adoption 与 journal 原始字节，不得报告部署成功，也不得继续分析识别率。失败回滚必须先确认新进程退出；Windows 已无匹配进程但映像或目录句柄仍在释放时，只允许在固定 3 秒预算内重试删除新安装和原子恢复旧目录，永久错误或超时继续 fail closed。

正式部署只更新既有 `C:\Users\apple\OneDrive\Desktop\Hextech伴生终端.lnk` 到 `C:\HextechCompanion\Hextech伴生终端.exe`；仅处理指向稳定安装或本仓 release 的重复快捷方式。`C:\HextechCompanion.previous` 始终保留部署前的一代正式版本，作为唯一紧急回滚目录。

## 真机验收

- 至少 50 个合格 active Hextech captured recognition observations；报告必须分别给出 capture、recognition 与 total，单帧捕获加识别 P95 ≤ 180 ms；FP32 镜像预热 ≤ 3 秒，Sidecar 峰值工作集 ≤ 1.5 GB。
- 首个完整三槽呈现 P95 ≤ 900 ms。
- 至少 20 个独立 `event_written_at → presented_at` 样本，Host event→present P95 ≤ 100 ms。
- 瞬时错误候选不得撤下稳定槽，鼠标只冻结实际覆盖槽。
- Hextech fresh 时不得因 Apex/Mayhem last-good 显示“上一代数据”；复仇焰魂命中“虚幻武器”等已知方案时必须显示当前英雄联动。
- 默认会话不得生成 PNG。
- 游戏期间不得存在 acquisition worker 或 Scrapling 逐请求摘要日志。
- EXE、manifest、Desktop、Sidecar、Overlay host 和 session report 的 `build_id` 必须一致。
- `slot_starvation_count=0`；每个合格 epoch 都有 retained timeline v2、terminal marker、对应 session report，并且最多一张 Overlay-only 裁剪图；两次留存周期和一次应用重启后证据仍存在，`timeline_missing=0`、`FileNotFoundError=0`。

新排名源候选仍必须重新完成至少 5 局真机稳定门：至少 2 次碎片选择、15 次逐槽重随、5 次 Alt-Tab、2 次中途启动或重启；错误 READY、碎片海克斯展示、未变化槽破坏、旧身份回流、额外 revision、空白帧和错误 tier 展示都必须为 0。未通过时不得部署到稳定目录。

单元测试和 packaged smoke 只能验证状态机、契约和进程入口；真实 League 窗口下的捕获耗时、模板区分度和 P95 指标仍需部署新包后连续真机验证。

Augment/Arena 胜率、出场率网站、应用或 Overlay 只按用户决定保留为私人本机实验；依据 Riot 当前 Developer Policy，它是公开分发阻塞项。候选不得被描述为公开分发合规版本，不引入注入、游戏内存读取、Vanguard 绕过或新的第三方 Overlay runtime。

## 桌面响应式三档与本轮验收边界

- 实际逻辑宽≥300为normal（三列开关、横向指标），230–299为narrow（两列开关、指标另起一行），200–229为minimum（单列开关、指标另起一行）。变窄立即重排，扩展需超过断点8逻辑像素。窄幅标题/关闭保留首行，刷新/诊断换行；英雄名最多两行并提供完整名称悬停提示，状态按真实字体度量最多两行，不再依赖18字常量。
- 字体、头像尺寸、列表行及按钮同步应用布局；固定操作区和状态区先分配空间，只有列表滚动。重排保留控件身份、开关、忙碌状态及首个可见英雄偏移，不为重排读取统计或启动刷新。头像按原始资源重采样，尺寸版本不匹配的迟到结果丢弃。
- 桌面后台回调经有界队列进入GUI owner，后台不触碰Tk控件或PhotoImage。映射期NOACTIVATE保留到下一消息周期再恢复，过期映射回调无效。
- 诊断包含逻辑/物理尺寸、DPI、客户端比例、档位、最小尺寸和受限原因；Overlay包含安全区、空隙档位、字号和可用的真实Canvas bbox。协议主版本不变，不新增默认PNG。
- 自动验证覆盖桌面四屏×五DPI×四客户端宽度×四位置情形、三档原生控件及游戏80组参数布局；合成窗口模式与模拟DPI不是实际League/混合DPI显示器验证。缺少真实各规格各稀有度内框帧及双屏选人/五局证据时，候选只能标记未部署、真机待验收。
