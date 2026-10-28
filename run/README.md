# Hextech 伴生终端

当前源码为三进程、release v4 重构候选；尚未替换正式安装，也不代表真实游戏验收通过。

隔离候选由包内 `bundle_manifest.json` 的 `runtime_profile=isolated_candidate` 固定使用 EXE 同级 `var`；直接 EXE、BAT 和桌面测试快捷方式使用相同目录。正式包的 `installed` profile 继续使用用户数据目录。冻结包不接受 `HEXTECH_VAR_DIR` 改写路径，配置损坏或目录不可写时明确报错，不回退正式数据目录。

产品保留桌面备战席、托盘、游戏内识别、阶段统计、Apex/Mayhem 联动和自动更新。Web 页面、HTTP 后端、浏览器跳转及旧 CSV/Blitz 运行链已退出。

## 启动与验证

Python 3.11，依赖以 `pyproject.toml` 为准。安装 editable package 后运行：

```powershell
python -m pip install -e .[dev]
hextech-desktop
```

也可使用根目录 `启动Hextech.ps1`。生产由主程序管理 Overlay 和 Vision，不分别启动旧 DataService/Supervisor，也不启动 Web。

```powershell
python -m tooling.checks.dev
python -m tooling.build --help
python -m tooling.acceptance.smoke_packaged_startup --help
```

隔离 Vision 诊断仍可使用以下显式入口；它们不是生产进程管理入口，也不能证明真实游戏验收：

```powershell
.venv\Scripts\python.exe -m hextech.infrastructure.vision.sidecar --once --preset auto --write-event
.venv\Scripts\python.exe -m hextech.infrastructure.vision.sidecar --loop --preset auto --write-event
```

## 运行结构

- App：备战席与托盘；后台数据刷新、Context、唯一生命周期与诊断留存调度。
- Overlay：独立 GUI，保留 HWND、DPI、前台显隐、捕获排除和本局统计冻结。
- Vision：独立捕获/OCR/时序识别；通过有界管道上报事件，不把截图传入 GUI 通道。
- 来源 worker：按需启动，完成或取消后退出。

App 与两个子进程各持有一条有界管道，Context 与选择事件不再依赖高频 JSON 文件通信。网络、文件校验和报告写入均不进入 GUI 线程。

## 数据

腾讯官方 `kiwi_augments.json` 决定正式 KIWI 身份（精确模式、非 PBE）；CommunityDragon 只补充资源。ARAMKit 提供排行和单英雄 all/Stage 1–4 统计；Apex/Mayhem 提供联动。

`var/snapshots` 的 v4 release manifest 引用不可变 Catalog、排行、英雄和联动单元，不复制整库展示数据。Desktop 与 Overlay 使用同一个只读查询接口；英雄数据按需读取并使用容量二的 LRU。更新名单与本局统计采用独立，首次选择截止后不更换本局统计。

新版运行包只接受 v4。旧 v3 需由离线转换工具导入独立目标目录并验证；更旧资料和人工证据原样保留。没有自动迁移、清理或原地覆盖旧运行数据。

识别名单可先于统计更新。切换统计绑定的 Catalog 时，新核心统计完整后才替换已有 release，下载中断继续保留旧统计；同一 Catalog 内仍按英雄增量发布。

## 代码与资产边界

`src/hextech` 包含 contracts、modules、interfaces、infrastructure 与 bootstrap。组装只在 bootstrap；业务模块不反向依赖实现适配器。`resources` 为只读资产，`var` 为运行数据，`tests` 为隔离测试，`tooling` 为开发、构建与离线诊断入口。

详细规则见 [文档索引](docs/README.md)、[系统设计](docs/system-design.md)、[数据布局](docs/data-layout.md) 与 [运行合同](docs/overlay-runtime.md)。旧格式和旧流程文档仅用于历史证据阅读，不能恢复为生产 fallback。
