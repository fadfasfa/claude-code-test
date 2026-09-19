# 版本驱动检测与更新

2026-09-16 源码合同；未打包或部署，不代表运行中的 rf9 已采用本改动。

## 周期不是有效期

| 来源 | 正常检测周期 |
| --- | --- |
| ARAMKit / Apex / Mayhem | 4小时 |
| Blitz | 2026-09-19 起退出新运行调度；历史记录只读兼容 |
| Catalog | 24小时 |

周期只决定下一次检查，不决定全量下载、数据失效或 generation 发布。`data_at` 仍是上游生成时间；最新性来自绑定版本的成功检查证据。几天前的数据可以仍与上游一致。“与上游一致”指最近一次成功检查，`last_checked_at` 与数据时间分别记录。程序不运行时不新增系统后台服务。

## 状态与请求

- 复用 `state/data-service/refresh_schedule.v1.json`，兼容增加 `check_status/upstream_revision/applied_revision/last_checked_at/check_interval_seconds/consecutive_failures/failure_fingerprint`。旧记录没有足够证据时按 unknown/never_checked 读取，历史 manifest 不改写。
- ready 的 Apex/Mayhem 旧72小时计划按最近成功检查加4小时收敛；过期只补一次检查，不补跑历史周期。backoff 原截止不被迁移缩短。重启沿用计划和失败指纹。
- 普通手动 force 只跳过检测等待，不跳过版本比较。Core singleflight；Optional 合并手动检测进入原串行线程，不新增并行来源任务。
- 条件请求缓存位于已有 runtime 下的 `state/http-validators`，按来源、URL、参数与表示请求头分区。304只复用哈希/大小校验通过的正文；缓存缺失或损坏，最多补一次无条件GET。error/error_kind存在时不得授权304复用。
- 固定dataPath原文继续使用原RawResponseCache。条件缓存默认单响应32MiB、每来源正文6GiB，来源级锁预留正文原子写峰值（manifest/锁元数据不计正文预算）；超限拒写，不自动删除旧/未知资产。祖先和子项拒绝reparse/junction，内容寻址摘要必须先验证格式。

## 来源行为

- ARAMKit轻量探测使用完整 `version_marker()`，调度/组件绑定revision仍为dataPath；不变且所有ranking/hero manifest、artifact、child及处理revision有效时，不启动下载worker。
- 缺损或parser/projection升级时，从已验证原始缓存补齐/重投影；坏immutable unit不覆盖，而使用新的repair身份，通过验证后采用。下载期间上游变版不得混合发布；外层旧probe不能盖过worker实际取得的新版本。
- Apex/Mayhem优先条件请求；没有可信轻量marker时仍需读取必要页面。原始表示revision与规范化业务revision分开，广告/时钟等不改变业务时不建新run。当前任务已取得正文直接用于解析，不做第二轮重复下载。
- Apex成功短路必须目标全集完整，所有响应可信；同正文503/timeout不能宣称最新。Mayhem先内存投影后决定发布，不因检查创建orphan raw run。
- 成功来源检查的业务revision与已采用revision一致时记录up_to_date。发现但未采用才是changed。Catalog/parser身份改变使旧投影失效，重新投影不等于重新下载。
- 相同验证失败指纹绑定版本、parser/projection和Catalog；相同失败不重复昂贵解析/建run。内容或处理条件变化后可恢复尝试。confirmed_empty不是失败。
- Mayhem 的失败指纹独立绑定完整验证输入（含rejects、分页及检查完整性），不是仅合法业务行的成功revision。上游修复非法条目后，即使合法业务内容未变，也必须重新验证；请求时间/304/Retry-After等不影响验证输入身份。

## 失败与显示

- 网络/临时故障独立按5分钟、15分钟、1小时、最多4小时退避并加少量抖动；服务端更长Retry-After优先，包括主进程ARAM marker probe和各worker路径。
- 验证失败默认6小时后再探测，不每5分钟重复全量构建。成功清除失败计数；失败不清空有效last-good。
- 自动due/core轮次先检查来源backoff截止，冷启动、缺失单元、缺失优先英雄或上下文变化都不能绕过失败退避；缺损只允许绕过普通ready检测间隔。显式force沿用既有用户手动重试语义，不自动提升为force。
- snapshot/推荐/阶段统计按当前runtime检查记录与immutable run/revision/Catalog绑定投影。不同英雄unit不能借ranking状态冒充最新，旧数据缺检查证据显示未知，不仅凭年龄标stale。未来异常检查时间不能授权最新。
- not_due不更新成功检查时间。某一来源检查成功不表示全部来源已检查。UI区分“正在检测”“与上游一致”“发现更新”“正在补齐”和“检测失败，继续使用已验证数据”。
- 原v2 seed严格验证不放宽：仅有旧fresh标签而缺检查证据的degraded seed不能冒充已确认最新；v3继续按完整unit闭包验证。局中统计冻结、识别矩阵切换、已确认身份及显示布局不变。

## 验收

离线覆盖42次正常检测（7天×每天6次）、状态恢复、304/200相同/变化、非业务页面变化、缓存丢失/损坏、版本漂移、parser/Catalog变化、坏unit安全修复、失败去重、服务端12小时Retry-After、旧72小时迁移和并发合并。分别检查请求、下载、run与generation行为，不用最终文案代替数据链路验证。

未联网执行真实刷新，未修改正式/测试运行数据、安装或快捷方式，未打包、部署、清理或Git发布。真实上游条件请求兼容性和运行效果仍须候选包验证。
