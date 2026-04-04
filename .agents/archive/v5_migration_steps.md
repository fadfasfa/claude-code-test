> **[迁移文档更新]**
> 本文件补充 V5.1 → V5.2 的迁移要点。

---

# Hextech V5.1 → V5.2 迁移要点

## 一、核心定位变化

旧设计：
- `agents.md = 不可变初始契约`
- 运行期扩范围、新需求只写 event_log
- 非契约任务默认不建 `agents.md`

新设计：
- `agents.md = 长期任务总表`
- 当前有效范围与运行台账允许追加更新
- 非契约代码任务也写 `agents.md`

## 二、分流方式变化

新设计改为三轴：
- `task_type = code | retrieval`
- `execution_mode = contract | ad-hoc`
- `branch_policy = required | on-demand | none`

## 三、合并后恢复变化

旧设计：
- 远端自动合并，分支删除
- 本地手动清理

新设计：
- 远端 merge 后自动归档当前任务卡并把 `agents.md` 重置为 standby
- 本地在下一次 `git pull` / `post-merge` / 同步脚本时自动对齐
