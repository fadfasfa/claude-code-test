# Hextech 数据布局

当前源码使用 release v4；正式安装和历史数据不因源码更新自动转换。

## 只读资源与运行目录

- `resources/catalog`、模型、模板与固定素材为只读基线。
- 新冻结包仅使用 `resources/seeds/runtime.v4` 内验证过的精确引用闭包。旧包seed保留为历史，不作为生产fallback。
- `var/catalog/generations/<id>`：不可变官方身份目录及已验证资产。
- `var/sources/<source>/runs/<id>`：不可变ranking、单英雄scoped stats、Apex或Mayhem单元。
- `var/snapshots/generations/<id>/manifest.json`：schema 4引用清单，不再另存整库champions/champion_hextech/overlay_hints/identities副本。
- `var/snapshots/current.v4.json`：一个原子head，保存统计current/previous及识别Catalog current/previous；两个消费角色可分别推进。
- `var/state/data-service/refresh_schedule.v1.json`：下一次检查、退避、错误指纹与来源证据。检查时间不是内容版本。
- `var/raw-responses`与`var/state/http-validators`：固定版本原文和条件请求正文，分别验证各自身份，不把可变URL当不可变版本。
- `var/cache/{overlay_vision,assets}`：可重建矩阵/图像缓存；`var/user-data/preferences`：用户偏好。
- `resources/evidence/mayhem_combos.raw.json`：既有可提交来源证据，不随本轮清退删除。

源码与测试可由`HEXTECH_VAR_DIR`指定运行目录；冻结包由包内`runtime_profile`决定，隔离候选固定使用EXE同级`var`，不接受父进程环境覆盖。测试conftest在导入时设置临时根。生产不通过旧CSV目录和旧Web端口文件发现数据。

## 发布与恢复

先写不可变单元，验证路径、大小、hash、Catalog身份及child闭包，再写release manifest；最后在统一锁下原子替换head。统计视图只消费head与其引用闭包，source current不是第二份发布权威。

识别Catalog更新不要求统计同时更新；本局已冻结的统计与活动视图引用保持保护。current损坏时只恢复完整有效previous，不能掩盖新输入损坏或混版。

只读客户端按运行根、统计 generation 和 manifest 实际字节摘要缓存最多两代查询对象；识别目录独立推进不使统计缓存失效。每次仍读取 head 与 manifest，首次加载及未缓存单元继续验证路径、大小和摘要，JSON 从同一批已验证字节解析。对外 DTO 与可变 manifest 字段复制，已验证内存不接受后续磁盘变更污染；未缓存或淘汰后重读的单元必须重新校验。

## 迁移与保护

离线转换只接受有效v3输入，验证后复制必要闭包到不存在的独立目标目录，生成v4；失败不得覆盖原始输入。旧v2、CSV、未知文件、旧安装和人工证据只读保留。源码重构不自动执行真实迁移。

当前、上一代及活动引用不可清理。旧source/Catalog/raw的生产者身份不明时保留；只删除已证明归本版本所有且不受保护的过期派生产物。不能把删release manifest的大小当作释放整个来源闭包。

## 诊断

生产热事件与Context走管道；诊断文件不能作为失效时的热输入fallback。保留低频状态、有限timeline、在线epoch摘要、session report及异常选卡ROI。

selection cache独立遵守200组/256MiB和人工/未知/部分写入保护；全局诊断由App调度。旧trace history不再写入，既有历史文件保持证据属性，不自动迁移或清空。
