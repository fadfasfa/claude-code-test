# 2026-09-19 后续时序、留存修复与发布边界

## 已修复

- shell draw/map之后，在同一Tk回合提交有界、非阻塞的数据准备请求；仍返回并保持shell，不在该回合消费后台结果或覆盖画面。
- 新增同publication首次event read字段，重复读取与Context独立完成不刷新／丢失该时间；不修改既有read新鲜度和TTL。prepared到draw携带完整输入计时，另记录请求、后台开始／完成、Tk消费时间，避免不同输入快照混合归因。
- 普通slot generation变化与成功重随不自动成为异常；未完成的新重随不在初始快照锁定anomaly。明确识别异常、慢确认、身份错绑及已锁定的真实异常等级保持保护。
- 独立审查否决了“根据最终快速READY降低旧anomaly”方案：稀疏最终帧无法排除先前真实异常。该分支已完全撤销，旧已持久化anomaly仍为300，真实异常coalesce→persist→普通组争用的回归保持原组与原字节。
- 全量回归暴露Windows文件锁竞争中的锁前缓冲写／close再次flush问题；改为OS锁取得前不写，持锁后无缓冲写PID再truncate到完整长度，不制造瞬时空文件。

## 独立审查与验证

- 独立预审通过真实32事件报告与内存复现发现多等一轮准备及混合计时；原165ms总延迟仍有效，旧72ms分段并非严格的首次Host观察延迟。
- 独立最终审查首先给出NO-GO，确认旧异常降级可误删真实异常；主线程修复后，独立定向53 passed并给出代码GO。随后文件锁补丁再次独立复核：30项相关测试、2项单实例／retention互斥测试和真实独立子进程的持锁拒绝／释放后获取均通过，最终代码GO，无未解决P1/P2。
- 首轮后续定向130 passed；增加Context复制回归后的42项通过；旧异常保护相关53项通过；文件锁、实时几何与新回归30项通过。
- 最终全量：2507 passed、19 subtests passed，405.90秒，0失败／0错误。记录：`run/.artifacts/repair-20260919/followup-final.xml` 与 `.log`。45056条既有依赖／Pillow警告未过滤。静态记录：同目录 `followup-final-pyright.log` 为0错误／1个既有警告；Ruff及diff-check通过。
- 不把自动测试或代码GO视为真实游戏P95≤100ms及五局验收通过。本轮没有修改识别阈值、字号或锚点。

## 交付／保护

用户已授权本轮run修复在main普通提交、推送和打包。根仓原有五处治理修改不进入提交；不创建分支／worktree／PR，不重写历史。

本轮发布前置检查发现真实游戏进程仍在运行。既有 `shutdown_for_package()` 明确以 `real_game_active` 拒绝在对局中关闭Hextech或启动原生smoke，因此提交推送可先完成，后续打包／稳定替换必须等游戏进程退出。不得绕过该检查或终止游戏。旧候选包的smoke不能替代这次后续补丁的新包验收。

部署条款适用范围经过独立复核：五局硬前置的主语为“新排名源候选”，另一处限制属于桌面响应式专项。本轮不新增排名源、不改桌面响应式／锚点，先前把它推广成全部修复的部署前置不准确，预审已撤回该判断。按用户本轮明确授权可在自动门及代码审查通过后使用现有部署器稳定替换；这不是豁免专项门。新Build真实P95与五局仍单列未验证，部署通过不等于真机GO。

正式安装、`.previous`、共享运行态、人工图像与标注、source/generation以及旧异常缓存保持保护。旧缓存的已存等级不能安全推断为误报，因此新代码只阻止继续误标；满额存量的清理需精确授权。

## 旧包候选清单（预览，尚未删除）

以下路径相对仓库根。仅在新正式安装核验通过、旧测试入口引用已处理且用户确认精确清单后删除；不处理父目录及旁边的appdata、测试证据或isolated runtime。

| 路径 | 预览字节数 |
| --- | ---: |
| `.artifacts/hx/releases/HextechCompanion-20260917-pr98/` | 356795748 |
| `.artifacts/hx/releases/HextechCompanion-20260917-pr98.zip` | 150526698 |
| `.artifacts/hx/smoke-pr98/HextechCompanion-20260917-pr98-185236/` | 356795997 |
| `run/.artifacts/fix19-package/releases/HextechCompanion-20260919-repair/` | 357049862 |
| `run/.artifacts/fix19-package/releases/HextechCompanion-20260919-repair.zip` | 150618776 |
| `run/.artifacts/fix19-smoke/HextechCompanion-20260919-repair-183906/` | 357049862 |

当前桌面“Hextech 测试版 20260919.lnk”仍指向第四项，故该目录目前仍在用并受保护。以上合计约1.61GiB；预览不代表已经释放空间。执行前需重新核对路径、重解析点、引用、进程和包身份。
