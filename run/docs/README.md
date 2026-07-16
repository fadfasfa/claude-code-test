# Hextech 运行区文档索引

本目录只保留当前系统事实源和已明确暂停的未来路线，不保存阶段计划、竞品 review 或重复架构说明。

| 文档 | 何时阅读 | 状态 |
| :--- | :--- | :--- |
| [system-design.md](system-design.md) | 修改桌面、Web、数据刷新、Runtime Supervisor、Python/Tk/Vision Overlay、打包或诊断前 | 当前事实源 |
| [overwolf-route.md](overwolf-route.md) | 评估未来是否以 Overwolf GEP/ow-electron 替换或补充 Python Overlay 时 | 暂停路线 |

维护规则：

- 当前生产路线是 Python 3.11 + Tk + Vision sidecar；实现与验收以 `system-design.md` 为准。
- Overwolf 不参与当前启动、fallback、打包或运行态数据链路。
- 行为、路径或预算发生变化时，代码、测试和对应设计章节必须在同一 PR 更新。
- 历史计划和审查结论若已被系统设计吸收，应删除而不是继续并列维护。

## 模块维护入口

| 新需求 | 首要修改模块 | 不应修改 |
| :--- | :--- | :--- |
| 新增统计来源或装备数据 | `data_core` pipeline、snapshot query | Desktop/Tk、Vision |
| 新增推荐规则 | `recommendation` | Web/Overlay 内重复计算 |
| 新增 LCU 状态 | `game_context`、`adapters/lcu` | Presentation 直接解析 payload |
| 新增识别方式 | `vision_engine`、Vision adapter | 统计查询和排序 |
| 新增展示面 | Presentation adapter | CSV、LCU、Vision 内部 trace |
| 修改进程启动/fallback | Runtime/Supervisor | Recommendation 和领域 ID |

跨模块数据先在 `hextech/contracts/` 定义版本化 DTO。`ChampionId`、`AugmentId`
和 `ItemId` 在 adapter 边界规范化一次，进入核心后不得再以 `int/float/任意字符串`
混用。

## 流程图索引

`system-design.md` 内的图是维护入口，不是展示性插图：

1. 模块依赖方向：判断新代码应落在哪个边界。
2. 进程与线程：排查 PID、Job Object、管道和 UI thread。
3. Overlay warm/cold 与 Web fallback：排查 30/60 秒预算。
4. Snapshot bootstrap/refresh：排查“数据准备中”和 last-good。
5. LCU 到客户端悬浮窗：排查英雄缺失、排序和角色高亮。
6. 游戏窗口到 Vision/hover/Overlay：排查悬浮消失和部分识别。
7. Session 状态机：排查等待态、降级态和错误显隐。
8. 源码 UI 与便携包 runtime：排查同代码不同运行数据。
9. 装备与选人海克斯推荐扩展：新增需求时避免把规则写进 UI。
