## 工作范围 (agents.md) — V6.0

### 范围

项目路径：C:\Users\apple\claudecode\run
执行环境：Antigravity 轻量档（Gemini 3 Flash）
Branch_Name：ai-task-fix-augment-icon-map-20260320

Target_Files：
  - config/Augment_Icon_Map.json

### 目标功能

1. 访问 CommunityDragon 资源目录索引页：
   https://raw.communitydragon.org/latest/game/assets/ux/augments/
   获取该目录下所有文件名列表。

2. 在文件名列表中查找以下三个海克斯对应的正确文件名：
   - 暴击飞弹（当前映射值：CriticalMissile_small.png）
   - 升级：无尽之刃（当前映射值：UpgradeIE_small.png）
   - 逃跑计划（当前映射值：EscapePlan_small.png）
   匹配策略：忽略大小写，搜索关键词 critical、missile、ie、escape、plan 等。

3. 将 config/Augment_Icon_Map.json 中上述三条的值更新为正确文件名。
   仅修改这三条，不得改动其他任何字段。

### 验收标准

- 在浏览器中直接访问以下格式的 URL 均返回 200：
  https://raw.communitydragon.org/latest/game/assets/ux/augments/<新文件名>
- Augment_Icon_Map.json 格式合法（有效 JSON，无多余逗号）