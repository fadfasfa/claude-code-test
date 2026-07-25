# Hextech 伴生系统设计

## 模块边界

源码根为 `src/hextech`。`contracts` 定义跨边界 ID、DTO 和失败分类；`modules` 持有业务用例；`interfaces` 与 `infrastructure` 实现入站和出站适配；`bootstrap` 是唯一 composition root，负责具体进程与实现组装。没有无消费者的预留 package 或旧路径转发入口。

```mermaid
flowchart LR
    Modules["modules"] --> Contracts["contracts"]
    Interfaces["interfaces"] --> Modules
    Infrastructure["infrastructure"] --> Modules
    Bootstrap["bootstrap"] --> Interfaces
    Bootstrap --> Infrastructure
    Bootstrap --> Modules
```

反向导入由 architecture tests 阻断。模块不得依赖根脚本、旧路径 alias 或转发入口。

## 抓取与 generation 链路

```mermaid
flowchart LR
    Remote["CDN / Apex / Mayhem"] --> Fetch["Transport + Source"]
    Catalog["Catalog candidate"] --> Parse["Parser + Normalizer"]
    Fetch --> Parse
    Parse --> Run["var/sources/*/runs/run_id"]
    Run --> Gate["来源完整性门禁"]
    Gate --> Candidate["source candidate pointers"]
    Candidate --> Cohort["CohortPromotionStore + journal"]
    Cohort --> SourceCurrent["catalog/source current.v2.json"]
    SourceCurrent --> DataService["DataService generation builder"]
    Seed["resources/seeds"] --> DataService
    DataService --> GenerationGate["Schema / 数量 / SHA-256"]
    GenerationGate --> Generation["var/snapshots/generations/id"]
    Generation --> Current["snapshot current.v2.json（最后提交）"]
    Current --> View["固定 DataSnapshotView"]
    View --> UI["Desktop / Web / Overlay"]
```

只有 DataService 可以提升来源 current 并发布 generation。来源 publisher 只写 immutable run
和 candidate pointer，显式请求 direct promotion 也会失败；Web、Desktop 和 Overlay 都没有发布权限。

## 来源门禁

Hextech 以当前已验证的 Catalog contribution 为确定全集；首次安装可读取
`resources/catalog` seed。每个英雄只通过现有 CDN JSON 和静态 HTTP 详情链路取数；首轮成功项不重抓，失败项进入低并发尾部重试。缺英雄、串英雄、重复海克斯、字段类型或总行数异常都会使整次 run 失败。不接入 browser、stealth、登录态或新的第三方来源。

Apex 由稳定 slug map 直接构造英雄详情 URL。提取层分别返回结构化结果和有限错误诊断，合法空结果必须继续进入页面分类；结果只能是 `has_synergy`、有页面身份和明确空态证据的 `confirmed_empty`，或带 `FailureKind` 的失败。解析异常和未知空结果不能发布，Apex/Mayhem 只通过同一 cohort 原子晋升。

Mayhem 优先解析 manifest JSON，HTML 仅为结构化 fallback。reject 带稳定原因码和有限样本；空结果、规模回退或 reject 比例越界都保留 last-good。

公共 transport 统一记录 URL、backend、状态码、耗时、尝试次数、失败分类和可重试性。timeout、TLS、network 和 5xx 有限退避；403/429 按 host 熔断。不使用 stealth、验证码绕过、登录态或真实浏览器 profile。

## 进程与消费

- Desktop：Tk 控制面和用户操作入口。
- Runtime Supervisor：只管理 Overlay host、Vision sidecar、lease 与本机控制面，不拥有数据刷新职责。
- Bootstrap/Desktop service manager：组装并持有 DataService、Web 与 Runtime Supervisor 的具体进程。
- DataService：刷新来源、选择 last-good、构建并发布 generation。
- Web/Overlay：只通过固定 snapshot view 查询，不直接读取来源 run。

DataService 同时只运行一个 refresh cycle。运行中收到的普通触发合并为一次
`pending_recheck`，force 触发会升级该 recheck；当前周期结束后立即重新计算到期状态。
shutdown 会拒绝新触发并清除 pending，不启动后续 worker。

存在可用 current generation 时，对局期间的自动刷新和普通手动刷新统一延后；活动 worker 在检测到游戏开始后协作取消。对局结束 30 秒后只恢复一次合并请求，冷启动无 snapshot 时仍允许刷新。延后状态不等同于 stale 或失败，不进入 backoff。

Overlay 在有效的 `session_id + selection_epoch` 首次渲染时固定 immutable view。同一轮中
current 变化只记录 `new_generation_available`，下一 epoch、新 session 或本轮隐藏后才采用新代。

Vision 使用 FP16 存储矩阵与进程内 FP32 计算镜像的双层结构；Sidecar 启动阶段预热镜像，单帧按四个识别通道批量投影三槽。稳定槽采用 strong/medium/weak 证据分级、首次确认与替换双门槛以及滞回保持：weak 不授权展示，替换已有结果至少需要连续三帧。鼠标只冻结 `cursor_over_slots` 指定的槽，同帧多槽变化只产生一个 selection revision。Host 立即绘制已就绪的部分结果，并严格区分识别、身份、来源、当前英雄、上下文和 snapshot 六类缺失原因。

generation 构建先生成 Hextech hints，再用 Catalog 补全最终身份集，最后把当前 generation 的 Apex/Mayhem 联动按名称、规范化名称和 alias 投影到 hint。Catalog-only 海克斯可携带联动，但不会获得伪造统计。发布前验证联动投影覆盖和 last-good 回退；Overlay 数字行只读取 Hextech freshness，Apex/Mayhem freshness 仅影响联动区域，聚合 health 不再污染行级文案。

会话报告、latest、20 条轮转和 diagnostic 截图由有界单线程写入器异步处理，Tk 主线程只入队。默认不截图。事件、Sidecar、Host 和报告都携带 manifest v3 的 `build_id`，Desktop 会显示构建身份并报告组件不一致。完整运行与验收规则见 [overlay-runtime.md](overlay-runtime.md)。

源码态 supervisor 子进程显式继承 `src` import path；冻结态复用同一可执行文件的模式参数。所有运行日志写入 `var/logs`，诊断写入 `var/reports`。
