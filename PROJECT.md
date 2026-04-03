# 项目记录

## 2026-04-03

### 本次修复

- 将 `run/config` 下的文本映射配置统一为 UTF-8 无 BOM，避免编辑器把中文键名误显示成乱码。
- 补充 `run/config/README.md`，说明各映射文件的用途、来源和编码约定。

### 说明

- `Augment_Apexlol_Map.json` 是海克斯/强化符文外链图标映射缓存，启动时由 `run/web_server.py` 预热并在需要时刷新。
- `Augment_Full_Map.json`、`Augment_Icon_Map.json`、`Champion_Core_Data.json`、`Champion_Synergy.json` 是本地静态数据文件，供查询和展示逻辑读取。
- `augment_icon_source.txt` 用于记录当前图标来源标记，供启动时判断是否需要强制刷新缓存。

## 2026-04-02

### 本次修复

- 修复 `run/hextech_query.py` 中 `build_default_aliases()` 的缩进错误，恢复启动链路。
- 保持英雄别名合并逻辑为：
  - 官方英雄数据优先
  - `hero_aliases.json` 的别名补充
  - 内置别名表兜底
- 让 `run/web_server.py` 的导入链路不再被语法错误阻断。

### 验证结果

- 代码结构已重新整理，肉眼检查确认缩进块闭合正常。
- 本地 `Python313` / `Python311` 进程启动受环境模块缺失影响，无法完成完整 runtime 启动验证。
- 当前清理动作已准备收尾，工作流可回到待机状态。

### 备注

- `agents.md` 已恢复为待机模板。
- 已清理 `.ai_workflow` 中可见的无用运行痕迹文件。
- 本次执行对 `run/`、`heybox/`、`QuantProject/`、`subtitle_extractor/` 及若干根目录脚本的注释做了统一化整理，保留关键意图说明，压缩冗余注释。
- 由于当前环境的 `git` / `python` 可执行链路存在模块加载问题，未能完成分支创建和本地编译验证。
