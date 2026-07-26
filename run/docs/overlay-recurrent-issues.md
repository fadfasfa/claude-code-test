# Overlay 反复问题与修复方案回顾

本文归纳 Hextech Overlay 模块 git 历史中反复出现的问题与最新修复方案，供后续迭代避免重犯。规则与运行契约的事实源仍为 [overlay-runtime.md](overlay-runtime.md)，本文是问题史与反模式总结，不重复运行手册的现行规则。文件改动覆盖 [run/src/hextech/interfaces/overlay/](../src/hextech/interfaces/overlay/) 与 [run/src/hextech/infrastructure/vision/](../src/hextech/infrastructure/vision/)。

> 快照时间：截至 2026-07-26，含工作区未提交改动。commit 轨迹从 `a27c0e3`（overlay 初稿）到 `bf2d91d`（最新合并）再到当前未提交的"第二阶段"改动。

## 一、总览（按同类修复次数排序）

| 主题 | 修复次数 | 最新 commit | 核心反模式 |
| :--- | :---: | :--- | :--- |
| Context 数据链断裂 | 6 | 8a448e6 | 多源写入冲突、写入者归属不明 |
| 生命周期/退出竞态/Sidecar 存活 | 6 | bf2d91d | 生命周期多所有者、关闭中误启动、PID 复用误判 |
| 显隐抖动 | 5 | bf2d91d + 未提交 | 帧计数防抖、bool 二态探测 |
| 槽位错配 | 4 | 未提交 | 连续帧计数 ≠ 正确、跨局身份丢失 |
| 缓存 / 预热状态 | 4 | e346a7d / 4477582 | 旧 cache 残留、预热未完成就被消费 |
| Generation pin / 旧数据路径 | 4 | e204197 | 旧路径 fallback、对局中切换 generation |
| 场景门误清空稳定槽 | 3 | bf2d91d + 未提交 | 帧计数防抖 |
| 联动投影 / 分来源 freshness | 2 | 8a448e6 | 上下文错配、last-good 误判"上一代" |
| 报告写入阻塞渲染 | 2 | 8a448e6 | Tk 渲染线程同步写 JSON |

## 二、高频反复问题详述

### 主题 A：槽位错配与识别抖动（4 次，根因同源）

症状演进：双字体冲突时放弃识别 → 旧局卡名残留到新局 → 同名视觉版本反复切换闪烁 → 双字体一致但指向错误卡名（如"玻璃大炮"被认成"巫师"）。

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 01cb18d | 双字体冲突直接放弃，槽位长期空置 | [matcher.py](../src/hextech/infrastructure/vision/matcher.py) `candidate_from_slot` 对冲突只有"放弃"一条路 | 引入 `dominant_text`：冲突时置信度≥0.95 且 margin≥0.10 取优势字体 |
| 2 | bc03077 | 旧局名称残留到新局；同名视觉版本反复切换 | 用 `augment_id` 跨帧跟踪，同名不同视觉版本被当成不同候选反复刷新 | 引入 `recognition_key`：跨帧只跟踪 `normalize_augment_id(name)`，同名统一身份；图标强证据才消歧版本 |
| 3 | c3874f0 | 跨局错配 | [state.py](../src/hextech/infrastructure/vision/state.py) 无跨局身份识别 | 新增 [context_gate.py](../src/hextech/interfaces/overlay/context_gate.py) + `game_instance_id`，跨实例清空 epoch |
| 4 | 8a448e6 → 未提交 | 双字体一致但系统性误匹配（连续 2 帧就确认） | 连续帧计数无法区分"通道重复"与"独立佐证" | 8a448e6 引入 `evidence_grade`（strong/medium）+ 滞回；未提交改动彻底砍掉单字体/优势字体候选路径 |

最关键的反复教训（matcher.py 未提交改动注释原话）：

> "真机证据表明重复观察只会复制同一系统性误匹配，并不会增加独立信息。"

这是 4 次修复的收敛结论——单字体即使置信度很高也不能独立形成 ready。从 01cb18d 的"dominant_text 容忍单字体"→ 8a448e6 的"单字体可产生 medium"→ 未提交的"单字体完全不产生候选"，经历了三次收紧。

### 主题 B：场景门丢失误清空稳定槽（3 次）

症状：玩家 Alt-Tab、按计分板 Tab、鼠标悬停遮挡时，场景门短暂消失，已稳定的槽被清空，回来后重新识别。

| 第 N 次 | commit | 修复 | 失败原因 |
| :--- | :--- | :--- | :--- |
| 1 | 01cb18d | `residue_hold_frames = 2` 帧防抖 | 帧计数依赖帧率 |
| 2 | edaf6fb | `selection_click_armed` + `hover_occluded` 收紧 | 仍是帧计数（`absent_frames`） |
| 3 | bf2d91d + 未提交 | 真实时间分级宽限：全 ready 1.5s、部分 6s、空 0.75s；`scene_lost_at` 时间戳 | — |

未提交改动的关键细化：新增 [state.py](../src/hextech/infrastructure/vision/state.py) 的 `pause()` 方法替代 `block()`——`scoreboard_key_down`、Alt-Tab、窗口短暂最小化走 `pause()`（隐藏 overlay 但保留 `stable_slot` 和 `epoch`），只有持续 3 秒以上的 `capture_unavailable` 才走 `block()`（reset）。`transient_pause=True` 贯穿 [host_sync.py](../src/hextech/interfaces/overlay/host_sync.py) → [host_visibility.py](../src/hextech/interfaces/overlay/host_visibility.py) → `decide_visibility`，隐藏窗口但不触发 `active_scene_without_window` 失败态。

### 主题 C：显隐抖动（5 次）

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 8cd7e49 | 窗口在但不显示；切后台残留 | 显隐逻辑内联在 host.py；`gameflow` 只返回 bool | 抽离 `decide_visibility` 纯函数 + `WindowTargetPoller` 后台缓存 |
| 2 | f748d12 | LCU 不可用时 overlay 消失 | `probe_gameflow` 返回 bool，不可用=false | `GameflowState` 三态（IN_PROGRESS / NOT_IN_PROGRESS / **UNKNOWN**），UNKNOWN 显示 waiting |
| 3 | 255aaa4 | Tab 后恢复时机错；旧事件误显示 | `selection_window_active is None` 等价 False | `stale_event_hold` 1s 缓存事件 |
| 4 | bf2d91d | 场景消失立即隐藏，看不到结果 | 帧计数防抖太短 | 真实时间分级宽限（见主题 B） |
| 5 | 未提交 | Alt+H 切显隐与 `user_enabled` 冲突 | `toggle` 热键直接改 `visibility["user_enabled"]` 绕过 Supervisor | 移除 Alt+H，只保留 Alt+J 切模式 |

反复模式：bool 二态探测无法区分"不可用"和"不存在"。三态枚举（UNKNOWN / error / missing）是已验证的正确方向。

### 主题 D：生命周期、退出竞态与 Sidecar 存活（6 次，最高频之一）

退出竞态子线：

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 26f7824 | 关闭后 15-18s 才响应 | `ServiceManager.shutdown` 顺序 stop 阻塞主线程 | shutdown 改后台 daemon 线程 + 5s 超时 |
| 2 | c28ba6e | 关闭后 watchdog 仍重启 overlay | on_close 未设 `_closing` 标志 | `_closing` + `_shutdown_requested` + 线程登记 |
| 3 | 5d92c8e | 关闭中再调 start 启动将死进程 | `_shutdown_requested` 只挡 watchdog 不挡 UI toggle | `start_game_overlay` 内检查标志直接 raise |
| 4 | b0967dd | bootstrap 线程与关闭并发双重 shutdown | 关闭线程和 bootstrap 线程各持 service_manager 引用 | `_publish/_take_service_manager` 三态所有权转移 |

Sidecar 存活/启动子线：

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 63fc5b8 | 渲染失败无限重试刷屏 | 无退避、无优雅退出 | 指数退避 + 退出信号文件 |
| 2 | 9f3ea7b | ready 文件无法区分"未好"和"失败" | catch-all 异常 | `_classify_start_failure_kind` 分类 |
| 3 | bfc1d7e | sidecar readiness 前退出原因不明；死循环重启 | 只查 exit_code + ready 文件 | bootstrap 文件写结构化错误，区分 retryable |
| 4 | bfc1d7e | ServiceManager 和 RuntimeSupervisor 都管 overlay，watchdog 冲突 | 两个所有者 | 生命周期完全委托 RuntimeSupervisor，ServiceManager 只报告 |
| 5 | bfc1d7e | PID 复用导致假 running | 只查 `process.poll()` + heartbeat | `generation` UUID + `pid_started_at` OS 创建时间校验（≤2s） |
| 6 | bf2d91d | stale 后无自动恢复；UI 闪现"识别异常" | `_read_sidecar_liveness` 内联 80 行、不触发重启 | 抽离 [sidecar_liveness.py](../src/hextech/interfaces/overlay/sidecar_liveness.py) 纯函数 + Supervisor 检测 stale 自动重启，重启前先设 `starting/sidecar_restart` |

反复模式：生命周期多所有者必冲突；`_closing` → `_shutdown_requested` → 所有权转移三态锁是关闭中误启动的解。

### 主题 E：Context 数据链断裂（6 次，最高频）

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 4985798 | 写 context 重复加载英雄数据 | `_resolve_champion_name` 每次重载 core_data | `@lru_cache(maxsize=256)` |
| 2 | a10f149 | 悬浮窗拿不到当前英雄 | 无常驻英雄获取 | `OverlayContextPoller` 线程轮询 LCU |
| 3 | 01cb18d | LCU 断连即丢上下文 | 断连即清空 | Live Client 2999 同步 + `PRESERVE_CONTEXT_ON_MISSING` 30s 保留 |
| 4 | edaf6fb | 上下文健康状态不标准 | 多来源直写 | `ClientContextProvider` / `TypedGameContextProvider` 标准化 |
| 5 | c3874f0 | 跨局英雄错配 | 无上下文门禁 | [context_gate.py](../src/hextech/interfaces/overlay/context_gate.py) + [context_broker.py](../src/hextech/interfaces/overlay/context_broker.py) + `context_ok` 校验 |
| 6 | 8a448e6 | 联动数据错配、last-good 误判 | 无投影门禁 | [synergy_projection.py](../src/hextech/modules/recommendation/synergy_projection.py) 99% 覆盖门禁 + 分来源 freshness |

### 主题 F：缓存与预热状态（4 次）

| 第 N 次 | commit | 症状 | 根因 | 修复 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 81743b1 | 启动状态与缓存不一致 | 启动状态散落 | 收敛到 sidecar + single_instance |
| 2 | 31052f5 | 模板矩阵重复构建 | 无持久 cache | v2 cache（`TEMPLATE_RUNTIME_CACHE`） |
| 3 | e346a7d | v1 旧大文件残留、可能被误读 | v2 上线后 v1 未清 | v2 ready 后 `_cleanup_legacy_template_runtime_cache` 隐式删 v1 |
| 4 | 4477582 | 数据层 cache 不随 generation 更新 | 无 generation 绑定 | [generation_pin.py](../src/hextech/interfaces/overlay/generation_pin.py)：`SelectionGenerationPin` 持有 immutable snapshot view |

### 主题 G：Generation pin 与旧数据路径（4 次）

| 第 N 次 | commit | 修复 |
| :--- | :--- | :--- |
| 1 | a4d8843 / 35651f6 | 移除旧资源目录 |
| 2 | 4477582 | 新增 generation_pin.py（同轮只用首次 view，current 变化下一轮才采用） |
| 3 | e204197 | 治理旧数据路径与运行时一致性（PR#90） |
| 4 | ae68677 | generation_pin 加固 + 抓取/晋升收敛 |

### 主题 H：报告写入阻塞渲染（2 次，已在 8a448e6 解决）

[report_writer.py](../src/hextech/interfaces/overlay/report_writer.py)（8a448e6 新增 191 行）：单线程有界队列异步写入，Tk 渲染线程不执行 JSON 写入/历史轮转/截图；队列满时合并同状态任务。状态文件暴露 `report_queue_depth` 和 `report_dropped_count`。

## 三、最新修复方案（bf2d91d + 工作区未提交改动）

bf2d91d 是已合并的最新 commit，未提交改动是它的"第二阶段"——bf2d91d 解决识别层时序仲裁，未提交改动解决场景生命周期配合和背景运行时恢复健壮性。两者是递进关系。

### 1. 时序仲裁器：M-of-N 证据窗口（state.py `_update_slot`）
- 不再用连续帧计数，改为最近 N 帧中 M 次命中：strong 3 中 2、medium 5 中 3
- 证据 6 秒过期，miss 不清零只淘汰过期证据
- 不同身份只有 strong 证据才能替换已稳定槽（不对称替换）

### 2. 候选准入收紧（matcher.py `candidate_from_slot`，未提交）
- 只剩三条产生候选的路径：真机指纹→strong、双字体一致+图标佐证→strong、双字体一致无图标→medium
- 单字体、优势字体、shortlist 完全不产生候选（只保留诊断）
- 双字体一致但有高图标冲突时返回 None

### 3. 非破坏性暂停 pause()（state.py，未提交）
- `pause()` 替代 `block()`：隐藏 overlay 但保留 `stable_slot`、`epoch`、`revision`
- 触发：`game_window_missing`、`game_not_foreground`、`scoreboard_key_down`、3 秒内 `capture_unavailable`
- 仅 `game_instance_id` 变化时才 reset epoch；持续 3 秒以上 `capture_unavailable` 才走 `block()`

### 4. 真实时间分级场景宽限（state.py `update`）
- `scene_lost_at` 时间戳；全 ready 1.5s、部分 6s、空 0.75s
- `scene_loss_confirmed` 区分于主动 `selection_completed`

### 5. Sidecar 存活检测模块化与自动恢复（sidecar_liveness.py + supervisor.py）
- 纯函数 `read_sidecar_liveness`，严格依赖注入（pid/started_at/now/create_time）
- Supervisor 检测 `status=stale` 自动调 `run_game_overlay_action` 重启
- 重启前 `prepare_sidecar_restart` 先设 `starting/sidecar_restart` 避免 UI 闪现失效文案

### 6. 背景运行时恢复 health 校验（background_runtime.py + 新增 background_runtime_diagnostics.py，未提交）
- 待机恢复后 `_wait_for_background_runtime_health()` 验证所有组件（data_service/supervisor/web/overlay/sidecar）就绪且 Build ID 一致，超时 15s 才放弃
- Host/Sidecar Build ID 与 `current_build_id()` 不匹配时报 `build_mismatch` 而非假 ready
- 新增诊断模块：有界循环日志（最近 200 条状态转换），写入失败不影响主流程

### 7. 诊断时间线（sidecar_diagnostics.py）
- `selection_timeline`：每个真实 epoch 的观察 JSONL，按 epoch 轮转（最近 20 个）
- `_DiagnosticEpochSampler`：每 epoch 前 5 次独立观察的 ROI 诊断
- `timing` 携带 `capture_started_at / captured_at / recognition_completed_at` 供时序仲裁消费真实时间

## 四、跨主题根因模式（反复犯错的本质）

把 8 个主题的根因合并去看，Overlay 反复犯错集中在 5 个反模式：

1. **帧计数防抖不可靠**——主题 A、B、C 都栽在这里。低帧率或帧率抖动下"连续 N 帧"行为漂移。已全部改为 wall-clock 时间。任何新防抖逻辑不得再用帧数。
2. **bool 二态探测**——主题 C、D。`bool` 无法区分"不可用"和"不存在"。三态枚举（UNKNOWN / error / missing + last-good 保留）是已验证模式。新探测点必须三态。
3. **单通道/单证据独立授权 ready**——主题 A。单字体即使高置信度也会系统性误匹配，重复观察不增加独立信息。只有跨通道佐证（图标 + 文字，或真机指纹）才能 strong。
4. **生命周期多所有者**——主题 D。ServiceManager 与 RuntimeSupervisor 共管导致 watchdog 冲突。bfc1d7e 确立 RuntimeSupervisor 为唯一所有者。这个边界不可再模糊。
5. **旧路径/旧缓存残留**——主题 F、G。每次新增 cache/路径都要补"失效条件是否完整"和"旧版本是否清理"。v1 cache 残留、旧目录 fallback 都是反复踩的坑。

## 五、避免重犯检查清单（迭代前对照）

- [ ] 新防抖/宽限逻辑是否基于真实时间（`time.monotonic()`）而非帧数？
- [ ] 新探测点（窗口/gameflow/sidecar）是否三态 + 保留 last-good？
- [ ] 识别候选是否需要跨通道佐证？单通道是否只进诊断不进 ready？
- [ ] 已稳定槽的替换是否限为 strong 证据？是否有滞回（medium 不撤 strong）？
- [ ] 新启动路径是否检查整条 `_closing` → `_shutdown_requested` → 所有权转移标志链？
- [ ] overlay 生命周期是否只由 RuntimeSupervisor 管理？ServiceManager 是否只报告？
- [ ] 子进程存活是否校验 PID 创建时间（防 PID 复用）？
- [ ] 新 cache 是否绑 generation？旧版本是否有清理路径？
- [ ] 报告/JSON 写入是否走有界队列异步？是否未在 Tk 渲染线程执行？
- [ ] 上下文是否经 context_gate 门禁？联动是否走 synergy_projection 99% 门禁？
- [ ] 真机验证是否核对了 EXE/manifest/Desktop/Sidecar/Overlay/session report 的 `build_id` 一致？

## 六、与运行手册的关系

- [overlay-runtime.md](overlay-runtime.md) 是现行规则与契约的事实源（识别规则、场景门宽限、时序仲裁、分来源 freshness、打包部署、真机验收）。
- 本文是 git 历史视角的"问题史与反模式"，用于在新一轮迭代前快速回看"哪些坑反复踩过"。
- 当两者出现冲突时，以 overlay-runtime.md 为准；本文应随规则演进而更新，不再准确的历史结论应及时修订或标注。
