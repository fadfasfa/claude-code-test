# Commit Review - wf-web-redirect-20260309

## 变更摘要
精简 UI 并统一重定向逻辑，移除废弃的网页开关功能。

## 文件变更清单

### 1. run/hextech_ui.py
**删除内容**：
- 第58-65行：网页开关配置读取逻辑（包含 1 个 try/except）
- 第111-115行：网页开关 UI 元素（Toggle 按钮）
- 第180-186行：英雄点击的防抖和开关检查逻辑
- 第376-385行：`toggle_web` 方法（包含 1 个 try/except）

**保留内容**：
- 所有核心功能（LCU 轮询、UI 渲染、数据同步）
- 所有现有的异常处理（psutil、requests 等）
- HTTP POST 请求逻辑（第187-191行）

### 2. run/static/detail.html
**删除内容**：
- 第160-165行：底部"英雄协同推荐"区域的 HTML
- 第431-505行：`renderSynergyArticles` 函数
- 第507-512行：`safeGet` 辅助函数
- 第562-600行：`loadSynergies` 函数
- 第558行：`window.onload` 中的 `loadSynergies()` 调用

**保留内容**：
- 海克斯数据展示的核心功能
- WebSocket 热更新逻辑
- Canvas 回退渲染引擎

### 3. run/web_server.py
**无变更**：智能路由分发机制（`/api/redirect`）已存在，无需修改。

### 4. run/static/index.html
**无变更**：主页无废弃 DOM，无需修改。

## AST 扫描警告分析

Git hook 报告的问题：
```
[METHOD REMOVED]  def toggle_web(self, event)  (was line 376)
[TRY REMOVED]     2 try/except block(s) removed (before: 13, after: 11)
[EXCEPT BROADENED] try block #8 at line 203: before=['(psutil.NoSuchProcess, ...)'], after=['Exception']
```

**分析**：
1. ✅ `toggle_web` 方法删除：**预期行为**，该方法属于废弃功能
2. ✅ 2 个 try/except 删除：**预期行为**，与配置文件读写相关
3. ❌ 异常处理扩大：**误报**，实际代码中 psutil 相关的异常处理未被修改

**误报原因**：删除代码导致行号偏移，AST 工具将第203行（删除后）误认为是不同的 try 块。

## 验证结果
- ✅ 语法检查通过
- ✅ 目标文件已正确修改
- ✅ 无核心逻辑破坏
- ⚠️ AST 扫描误报（需人类审核）

## 建议操作
由于 AST 扫描误报，建议人类执行以下操作之一：
1. 使用 `git commit --no-verify` 绕过 hook（需确认变更安全）
2. 手动审核代码后批准提交
3. 调整 AST 扫描工具的行号对齐逻辑
