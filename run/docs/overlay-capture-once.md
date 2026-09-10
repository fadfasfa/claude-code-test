# 游戏内显式单次诊断采集

本工具只用于用户显式开启的一次游戏客户区取证，不启动或连接生产 Sidecar、Host、
Desktop、OCR、tracker、网络服务，不修改正式 runtime、配置、旧日志或历史图像。
它与桌面右侧停靠无关，不负责部署或打包。

## 一键运行

在 `C:/Users/apple/worktrees/hextech-overlay-aramkit/run` 使用现有 Python 3.11 环境：

```powershell
./.venv/Scripts/python.exe -m tooling.diagnostics.overlay_capture_once --capture
```

终端显示一次倒计时；默认 3 秒内由用户手动切回前台游戏，随后仅尝试一帧。
可以显式使用 `--delay-seconds 5 --budget-seconds 8`；倒计时允许 0–10 秒，
子进程预算允许 1–10 秒，默认 5 秒包含启动、验证、捕获、分析及写入。
超时终止工具自己创建的诊断子进程，最多另留两次各 1 秒退出确认。
`worker_stopped=false` 表示回收未确认，不能继续重复采集或称成功。
不等待前台、不重试、不注册热键、不连续截图。

无 `--capture` 时退出码为 2，输出 `explicit_capture_required`，零窗口探测、零截图、
零证据写入。`--help` 也不会采集。成功退出码为 0，拒绝/失败为 1，参数错误为 2。

## 捕获和安全边界

- 只检查前台根 HWND，不枚举其他应用窗口、不读标题。仅允许进程名和 EXE basename
  均为 `League of Legends.exe`、PID/create time 完整且窗口可渲染的游戏；标题匹配、
  `window_fallback`、`hwnd_only`、最小化、缺失身份均不能授权。
- 严格查询客户区，不以外框或旧 rect 回退；要求独立采集进程成功进入 Per-Monitor V2，
  实时物理 DPI/显示器可读，窗口模式为明确的 Borderless/Windowed。Full Screen/unknown 拒绝。
- 生产 Sidecar 的 `ImageGrab` 路径是屏幕 ROI 联合捕获并可能再次回退，不满足此工具的
  单次及其他应用像素隔离要求。因此新增 vision 辅助，使用同一 Pillow RGB 图像体系，
  一次 `PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT)` 只绘制目标 HWND 客户区。
  不调用桌面 DC 的 BitBlt、不使用屏幕 bbox 回退。窗口/版式/显示锚点元数据复用既有模块。
- 前后采样同步绑定 HWND、前台 HWND、PID/create time、game instance、物理客户区、DPI、
  显示器和窗口模式。发现漂移时丢弃内存图像，不写 PNG；采样年龄最多 500ms。
  两个采样不是原子 OS 事务，不能证明中间每一瞬间前台都没变化；HWND 限定捕获避免
  在短暂遮挡时采入其他应用的屏幕像素。不要把它描述为生产 ImageGrab 像素等价验证。
- 单次最多 1600 万像素、PNG 最多 24 MiB。捕获失败、黑色/全平帧、尺寸不符、
  超时均失败，无备用捕获、无第二张截图。PrintWindow 在真实 League 上可能不可用或
  返回无效内容；非黑帧仍须人工确认，不能自动声称是真实有效游戏帧。

## 输出与证据解释

输出仅位于本工作树 `.artifacts/overlay-capture-once/<时间戳>-<随机UUID>/`，
固定输出根拒绝 junction/symlink，不提供任意路径参数。每次新建目录且文件独占创建，
不覆盖旧证据，不执行留存清理，不读取或写入正式 `var` 或用户 AppData。

- `client.png`：最多一张完整游戏客户区 PNG，仅供私人本机诊断；可能含玩家名字、聊天等
  游戏内信息，不自动上传、不提交为 fixture。
- `capture.json`：图像 SHA-256/尺寸/字节数、前后窗口快照、捕获起止 Unix 时间戳、
  单帧 `scene_observation`、原有显示布局身份与 `pending_real_device` 资格、采集器 Build
  及采集相关源码文件哈希。
- 源码执行的 `build.build_id=dev` 是明确的采集器身份，源码哈希补充当前版本绑定。
  **不是正在运行的正式 Overlay Build 证明**；`runtime_build_verified=false`。
  工具不读取旧 runtime 日志去伪造同帧绑定；主线程后续仍须核对正式 EXE/manifest/PID。
- `scene_observation` 复用原有 `detect_selection_scene` 单帧观察。`candidate`、`absent`
  都允许保存；不依赖 selection active、三槽 READY 或当前 Context 成功，因此能覆盖确认前阶段。
  `selection_confirmed=false`、`automatic_exemplar_eligible=false`、`requires_manual_truth=true`
  始终保留，不运行时序确认、不自动标真值、不猜名称、ROI 或锚点。
- 只有 CLI 返回 captured 且完整 JSON 可解析、图像哈希一致才是完整采集产物。
  超时或磁盘错误可能保留本次新目录/孤立 PNG/截断 JSON；这些不是合格证据，工具不自动删除。

## Context 原因拆分

`ContextRenderGate` 仍先拒绝缺少 game instance 或无效 HWND，原因保留
`context_game_identity_missing`。身份完整但 `active=false` 时改为
`context_selection_inactive`，仍 reset、清空英雄和确认状态，不 holding、不渲染。
恢复 active 后继续既有 publication 信任、身份绑定与确认门，不放宽任何安全条件。

## 自动验证与未完成项

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_overlay_capture_once.py tests/test_overlay_context_gate.py -q
./.venv/Scripts/python.exe -m ruff check tooling/diagnostics/overlay_capture_once.py src/hextech/infrastructure/vision/capture_once.py src/hextech/interfaces/overlay/context_gate.py tests/test_overlay_capture_once.py tests/test_overlay_context_gate.py
```

测试只用临时目录、合成像素及无截图阻塞子进程，覆盖默认禁用、candidate、身份/前台/
rect/DPI 漂移、窗口模式、预算、独占输出、输出根重解析点和超时回收。
这些测试不等于真实 League 捕获通过。真实客户区帧、人工真值、同规格显示内框校准、
混合 DPI/双屏/Alt-Tab 验收和正式 Build 关联仍缺；不猜 ROI 或锚点，不据此打包部署。
