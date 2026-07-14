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
