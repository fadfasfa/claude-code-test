# Overlay 反复问题与修复方案回顾

本文归纳 Hextech Overlay 模块 git 历史中反复出现的问题与最新修复方案，供后续迭代避免重犯。规则与运行契约的事实源仍为 [overlay-runtime.md](overlay-runtime.md)，本文是问题史与反模式总结，不重复运行手册的现行规则。文件改动覆盖 [run/src/hextech/interfaces/overlay/](../src/hextech/interfaces/overlay/) 与 [run/src/hextech/infrastructure/vision/](../src/hextech/infrastructure/vision/)。

> 快照时间：截至 2026-08-31，含工作区未提交的 v2/v3/v4/v5 修复。commit 轨迹仍以当前分支 HEAD 与不可变候选的 Build/source fingerprint 为身份；下文早期 commit 只用于解释历史演进，不代表当前真机运行版本。

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
| 快捷方式/Build 误归因 | 4 | v2–v5 | 点击快捷方式被误当成已切换进程和 Build |
| 呈现证明失真 | 3 | v3–v5 | Full Screen 下内部 READY/GetPixel 被误当成最终可见 |
| Timeline 留存与性能竞争 | 2 | v4–v5 | v1 占 v2 配额、writer/retention 双删除、全目录高频扫描 |

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
- `selection_timeline`：每个真实 epoch 的观察 JSONL；v5 起 writer 只 append，唯一 retention owner 只在 v2 集合内保留最近 20 个，v1/未知文件永久 pinned 且不占 v2 数量/字节配额
- `_DiagnosticEpochSampler`：每 epoch 前 5 次独立观察的 ROI 诊断
- `timing` 携带 `capture_started_at / captured_at / recognition_completed_at` 供时序仲裁消费真实时间

## 四、跨主题根因模式（反复犯错的本质）

把 8 个主题的根因合并去看，Overlay 反复犯错集中在 5 个反模式：

1. **帧计数防抖不可靠**——主题 A、B、C 都栽在这里。低帧率或帧率抖动下"连续 N 帧"行为漂移。已全部改为 wall-clock 时间。任何新防抖逻辑不得再用帧数。
2. **bool 二态探测**——主题 C、D。`bool` 无法区分"不可用"和"不存在"。三态枚举（UNKNOWN / error / missing + last-good 保留）是已验证模式。新探测点必须三态。
3. **单通道/单证据独立授权 ready**——主题 A。单字体即使高置信度也会系统性误匹配，重复观察不增加独立信息。只有跨通道佐证（图标 + 文字，或真机指纹）才能 strong。
4. **生命周期多所有者**——主题 D。ServiceManager 与 RuntimeSupervisor 共管导致 watchdog 冲突。bfc1d7e 确立 RuntimeSupervisor 为唯一所有者。这个边界不可再模糊。
5. **旧路径/旧缓存残留**——主题 F、G。每次新增 cache/路径都要补"失效条件是否完整"和"旧版本是否清理"。v1 cache 残留、旧目录 fallback 都是反复踩的坑。
6. **内部状态被当成用户可见**——Full Screen 下的 HWND、READY、Canvas draw 或 Desktop DC 探针不能单独证明最终扫描输出包含 Overlay。外部 layered-window 路线必须把 Borderless/Windowed 作为硬前置条件。
7. **同一资源存在多个清理所有者**——writer 和 retention 同时轮转必然出现配额误算和并发删除；所有删除必须收口到一个跨进程加锁、带共享节流的所有者。
8. **统计口径混入非目标 epoch**——candidate、body-shard、blocked、纯 pause 或不足一次 active 的临时 epoch 不能污染 Hextech 三槽覆盖率、首次 Canvas 或 recognition P95。

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
- [ ] 点击快捷方式不等于切换 Build；是否核对 EXE、PID、process start time、manifest 与单实例 owner？
- [ ] Full Screen 下是否避免把内部 HWND/READY/GetPixel 当作真实可见，并要求用户切换 Borderless？
- [ ] Borderless/Windowed 下是否在首次映射前回读 `WDA_EXCLUDEFROMCAPTURE=0x11`，且 packaged smoke 用 `ImageGrab` 高对比探针证明未被再次截入？
- [ ] AOC/BOE 是否按 Vision 变换后的真实卡框缩放文字、面板和间距，而不是直接乘 Windows DPI？
- [ ] legacy/current schema 的 pinned 文件是否完全不占新 schema 的数量与字节配额？
- [ ] 同一资源只能有一个清理所有者；跨进程是否有独占锁、共享节流和 active selection skip？
- [ ] 状态中的“已写入”是否只来自实际 append 成功，而不是仅计算出目标路径？
- [ ] packaged smoke、离线热测和空 runtime 是否没有替代预填充 runtime 与真实 League 验收？
- [ ] 部署窗口内到期刷新是否只接受完整校验、单调更新且不改变 Catalog/production pool 的 generation，并在失败时连 checkpoint/recovery/selection 一并回滚？
- [ ] 更新 runtime 命中 current receipt 时，是否仍先完整物化并验证新 Build 的 bundle baseline，且不倒退 current？失败回滚是否在确认进程退出后有界等待 Windows 句柄释放？
- [ ] candidate/body-shard epoch 是否从 Hextech 三槽覆盖率、首次 Canvas 和 P95 中排除并单列原因？
- [ ] 本地 recognition P95 是否没有替代真实 capture+recognition P95，且门槛仍为 180 ms？
- [ ] Augment/Arena win-rate overlay 是否仅保留为私人本机实验，并明确标为公开分发阻塞项？

## 六、2026 年 8 月 v2–v5 问题史

### v2：自动门通过但真机链路身份与可见性未闭环

- 自动测试、Host self-check 和 packaged smoke 证明的是受控进程链与内部合成，不是 League 最终画面。
- 开发快捷方式可能只唤醒既有稳定版单实例 owner；必须从 `TargetPath → process executable → PID/start time → manifest Build → runtime state Build` 逐层核对。
- candidate Build 的报告不能与稳定目录进程的报告混用；真实 session 必须绑定当前 Build、Sidecar instance 和 generation。

### v3：识别与 cohort 恢复收紧，但旧身份/旧路径仍需独立证明

- 稳定槽 transition 收口到明确点击、两个独立 content-absent frame 或 OCR exact 3/5，普通候选/fingerprint 漂移不再授权换卡。
- 冻结 bundle 改为验证完整 cohort 并单调恢复更新的本地 generation；旧 seed、缺 provenance 或伪 generation 必须失败关闭。
- 这些修复解决“错误/跳变”和代际倒退，不能自动证明 Overlay 在真实全屏链路可见。

### v4：启动和 Build 代际修复有效，最终呈现与 Timeline 留存仍失败

- 现场 `first_idle_visible≈148 ms`、receipt 命中、`data_ready≈2.50 s`，Host/Sidecar/Stats generation 一致，说明启动与代际路线有效。
- 真实游戏为 `WindowMode=0` Full Screen；把 `GetDC(0)/GetPixel matched` 称作 `presented` 超出了 Desktop DC 探针合同，无法证明最终扫描输出包含外部 Overlay。
- timeline 目录已有 20 个 v1；旧 retention 把 pinned v1 计入 v2 的 20-epoch/12 MiB 配额，新 v2 被立即删除。writer 与 retention 又同时轮转，产生并发 `FileNotFoundError`。
- 1043 次完整留存扫描造成 CPU、GIL 与磁盘竞争；70 个真实 captured observations 的 capture P95 为 66.684 ms、recognition P95 为 196.949 ms、total P95 为 263.470 ms，未达到 180 ms 门。

### v5：Borderless 硬门、单一留存所有者与合格 epoch 性能闭环

- 只读当前 League 安装根的 `Config/game.cfg`：Full Screen/unknown 时 Host fail closed，Sidecar 在截图前非破坏性 pause；Borderless/Windowed 后自动恢复，不写配置、不要求重启。
- `supported` 模式的空 reason 必须保持为空，不能用 `game_window_mode_unknown` 补默认值制造假警告；真实 unknown 仍 fail closed，但只进入结构化诊断与有限日志，不持续占用 Desktop 状态栏。
- Desktop DC 探针只在受支持模式运行并标记 `probe_contract=dwm_desktop_dc`；显式诊断只留 Overlay 矩形裁剪图，每个 active Hextech epoch 最多一张完整三槽 READY 图。
- `diagnostic_retention` 成为唯一删除/轮转所有者；v1/未知 timeline 永久 pinned 且不占 v2 配额，跨进程锁、60 秒共享间隔和 active selection skip 消除高频全目录竞争。
- 性能报告只统计目标 Build、同一 Sidecar instance 的 active Hextech epoch，并分别输出 capture、recognition、total、排除原因和最慢 10 个 observation 的结构化 `matching_timing`。
- 自动门、packaged smoke 与真实 League 门继续分开；首个 v5 真机若仍失败，保留证据并生成新的不可变 v5-r2，绝不覆盖候选或放宽 180 ms。

### v13：自捕获反馈环、同 epoch 非法换卡与双屏比例收口

- Overlay 映射后若重新进入 Sidecar 的 `ImageGrab` 输入，会压低卡面场景分并形成“显示 → 识别下降 → 0.75 秒后隐藏”的反馈环；顶层 HWND 必须应用并回读 `WDA_EXCLUDEFROMCAPTURE`，无法确认时 fail closed。
- Desktop DC/GetPixel 在不同系统上可能看见人眼合成像素或返回黑色，不能据此判断 affinity 泄漏；Host surface 用 Canvas 内容与 HWND 状态证明，桌面捕获排除由 packaged smoke 的已知底色高对比探针独立证明。
- 选择按钮仍存在且卡面/名称有残留时保持同一 epoch；按钮消失后才启动 0.75 秒宽限，明确卡面点击立即结束，重随点击只开启目标槽 replacement。
- 稳定槽只接受明确槽位点击、两个独立 `content_absent` 帧或不同身份 OCR exact 3/5；重复 strong/双字体候选与 fingerprint 漂移始终保留 last-good。
- AOC 2560×1440@100% 与 BOE 2560×1600@150% 以变换后真实卡框而非 DPI 为比例事实源；Tk 使用负像素字号，render cache 必须覆盖 viewport、DPI、`layout_transform` 与 geometry scale。

## 七、与运行手册的关系

- [overlay-runtime.md](overlay-runtime.md) 是现行规则与契约的事实源（识别规则、场景门宽限、时序仲裁、分来源 freshness、打包部署、真机验收）。
- 本文是 git 历史视角的"问题史与反模式"，用于在新一轮迭代前快速回看"哪些坑反复踩过"。
- 当两者出现冲突时，以 overlay-runtime.md 为准；本文应随规则演进而更新，不再准确的历史结论应及时修订或标注。

## 八、固定屏幕规格、后台准备与碎片回归

- v13 的“逐帧 layout_transform 驱动显示”已经被固定客户区版式替代。真机同一 revision 曾出现 29→32px 和约 29px 位移；保留识别布局也可能保留错误校准，不能作为显示比例事实源。
- 正常基准排除游戏自带魄罗指引；使用指引关闭的真实截图核对下方统计。顶部联动与游戏指引是不同 UI，不能混为一种数据源。
- 冷开 snapshot、全量 hint 深复制、scoped 校验放在 Tk 线程会阻塞已排队的映射/合成回调。新实现单后台线程准备数据，窄 hint 保留名称/评级/联动，不复制全英雄历史统计。
- 保留事件漏传 button_box 曾令已有联动变成 show_synergy=false；新显示禁入区由固定版式提供，不依赖每帧按钮检测。
- body_shard 曾只按两次漏检解除 latch，且诊断 writer/path 排除碎片，形成回归与证据缺口。现在持续保留到明确结束或真实时间确认，Host 再拒绝同轮旧 READY；碎片 observation/terminal 留存但不计性能分母。
- 检查新增缓存是否有身份栅栏、容量、关闭路径；同屏不变的内容/识别更新是否不会改框；原生 Tk 文字 bbox 是否真正位于固定面板内；自动 smoke 是否仍与真实游戏验收分开。

## 九、备战席遮挡与首帧占位尺寸

- 桌面空白长窗不是游戏内统计层。仅凭客户端前台显示会在大厅/结算弹出740px空窗；一次性withdraw没有关闭锁，下一tick会重新出现。
- 右侧不足320px时把x钳制到工作区右缘会覆盖客户端。历史版本曾试过8秒吸回、保持手动位置和邻屏停靠，均不是当前合同。2026-09-09确认继续同屏右贴，以 `desktop-stable28.md` 为唯一行为定义：最低200逻辑像素，不足时仅允许已有合法位置三秒调整宽限；不接受独立位置、不跳邻屏、不覆盖客户端。
- 后台窗口循环不得直接调用Tk显隐/置顶，必须投递最新观察给GUI owner。提前启动观察后，原先仅mock重服务的Tk测试需隔离新入口并在Destroy停止线程；UI/owner或Canvas/font循环会令Tcl被后台GC回收，不能靠跳过用例掩盖。
- 1×1是withdraw时的Tk占位，不是游戏尺寸。只记录target_rect而等窗口可见后才应用，会先画出反向统计框和8px字体，应先在隐藏状态应用已确认几何。
- 历史校准曾采用36px并扩展到卡槽全宽；作者现已选择30px及独立内框安全区，此旧字号方案不再适用。仅超宽行收紧排版空格，不缩字；Pillow的YaHei/YaHei UI字体face不同，离线差异不能直接当作真机字号差异。

## 十、桌面多屏响应式与首次HWND重建

- 固定320px外框加正磅值字体会在混合DPI下比例失衡；只按客户端宽度放大又会把右侧仍有320px空间的场景误判为不可显示。逻辑尺寸、实际右侧空间和最低可读宽度必须分别处理，三档重排不能隐藏英雄指标。
- Tk首次布局可能重建顶层HWND并丢掉旧句柄的NOACTIVATE，且UpdateWrapper会对首次窗口调用SetActiveWindow。布局、定位和映射使用当前GUI线程短作用域的WH_CBT/HCBT_CREATEWND保护，在新HWND创建时直接设置临时disabled/noactivate样式，返回后恢复并解除。不能在HCBT_ACTIVATE阶段否决，后者可能让旧前台变成NULL；不拦截游戏或其他进程，也不调用SetForegroundWindow把焦点抢回。
- 隐藏阶段从实际子控件请求尺寸计算最低高度，不提前驱动窗口消息；首次物理定位后才统一映射。映射后有版本校验的25ms回调恢复普通交互，旧回调不能激活新窗口。
- 原生离屏QA需先处理绘制消息并完整重绘再PrintWindow；缺少这一步可截入旧尺寸残影。此类捕获只针对隔离测试窗口，不是游戏实机图。
