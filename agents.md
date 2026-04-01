## 工作范围 (agents.md) — V6.1

### 范围

项目路径：.
执行环境：Claude Code
执行端标识：cc

Branch_Name：cc-task-skill-config-20260324

Target_Files：
  - .claude/settings.json

### 目标功能

1. 读取现有的 `.claude/settings.json` 文件内容。
2. 将共享技能路径 `"c:/Users/apple/.claude/skills/"` 注入到 `skillSearchPaths` 数组中。如果该数组不存在则创建；如果已存在该路径则保持现状。
3. 严格保留现有的 `permissions` 配置节点及其内部的所有 `Bash` 权限白名单（共 21 条），绝对禁止覆盖或删除原有权限配置。
4. 维持同目录下的 `.claude/settings.local.json` 现状不变。
5. 完成合并后保存文件，确保 JSON 格式合法。

---

## 待机态（/RECOVER 专用）

项目路径：[待填写]
执行环境：[待填写]
执行端标识：[待填写]
Branch_Name：[待填写]

Target_Files：
  - [待填写]

### 目标功能

[待填写]
