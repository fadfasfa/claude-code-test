## 工作范围 (agents.md) — V6.0

### 范围

项目路径：.
执行环境：Claude Sonnet 4.6 (Thinking)
Branch_Name：ai-task-fix-web-ui-startup-20260322

Target_Files：
  - run/hextech_ui.py
  - run/web_server.py

### 目标功能

1. 修复 run/hextech_ui.py 无法独立挂载/唤起 Web 界面服务的问题（通过子进程拉起 web_server.py）
2. 修复 run/web_server.py 中动态端口切换后，浏览器唤出依然指向旧端口的 Bug
