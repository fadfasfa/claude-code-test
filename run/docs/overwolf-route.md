# Overwolf GEP 暂停路线

## 1. 当前决策

Overwolf/ow-electron 路线已暂停，不进入当前产品、依赖、打包、运行态或 CI。当前正式路线仍是 Python/Tk/Vision。旧 `feature/hextech-overlay-overwolf-gep` worktree 仅用于一次技术探路，bridge 实现不保留；本文保存仍有效的技术结论，避免未来从零调查。

## 2. 已验证的技术事实

- `@overwolf/ow-electron` 可声明 `gep` 与 `overlay` package；package manager 提供 `ready`、`failed-to-initialize`、`updated`、`crashed` 等事件及日志目录。
- GEP 默认不监听游戏数据，应用 ready 后必须尽早注册 required features。
- LoL 常规 game id 探针使用过 `5426`，Arena 对照使用过 `21556`；这些 id 未来必须重新对照官方文档，不能视为永久合同。
- 探路目标 feature 为 `augments`，应同时监听 `new-info-update`、`new-game-event`、`game-detected` 并用 `getInfo(gameId)` 对照。
- GEP/overlay package 的首次下载与 ready 可能依赖官方运行时和 `content.overwolf.com:443`；应用不应静默安装客户端或修改系统环境变量。
- ow-electron 启动必须避免 `ELECTRON_RUN_AS_NODE` 导致退化成普通 Node；真实入口应确认 `electron.app` 存在。
- Electron renderer 应保持 `contextIsolation=true`、`nodeIntegration=false`，通过 preload 暴露最小 API。
- 探针输出若包含第三方 payload，必须递归脱敏 token/cookie/auth/secret 等字段，并写入 ignored 的 artifacts，而不是 Git。

## 3. 已完成但不迁移的探索

旧 bridge 曾验证：

- 普通 Electron 与 ow-electron BrowserWindow 可共用离线三卡 renderer。
- 本地 `augment.name-to-icon.v1.json` 与可选 `overlay_hint_cache.v1.json` 可做离线名称/图标/hint 映射。
- mock 三槽可以完成 renderer smoke 与截图。
- GEP dump CLI 可以设计 warmup、采样间隔、game detect 等待、call timeout 和全 feature 对照。

这些只是脚手架验证，不等于真实 GEP 三选一字段已确认。因此不迁移 Node 源码、package lock、mock 数据、renderer、脚本或对 Python 旧路径的 bridge 修改。

## 4. 未解决问题

- 当前目标模式下 GEP 是否稳定提供三个候选 augment，而不是只提供已选择结果。
- payload 中真实 augment 标识、槽位、时序、重复事件与 inactive edge 的合同。
- GEP for Overwolf Electron 对目标 LoL variant 的正式支持状态和审核/发布要求。
- 首次 package 下载、离线缓存、崩溃恢复和冷启动耗时是否满足产品预算。
- Overwolf overlay 与当前客户端悬浮窗、Web fallback、LCU/context 的所有权如何统一。
- 用户隐私、Riot/Overwolf policy、遥测和商店分发要求。

## 5. 未来恢复门槛

只有同时满足以下条件才新建独立 worktree 恢复探索：

1. 官方文档确认目标游戏/模式和 feature 支持。
2. 原生环境真机 dump 得到非空三候选 payload，并记录稳定 schema 与 inactive edge。
3. 完成 payload 脱敏、最小权限、离线/崩溃/升级边界设计。
4. 与 Python 路线做启动耗时、准确率、维护成本和发布约束对比。
5. 用户重新确认 Overwolf 成为候选正式路线。

恢复时应重新创建最小探针，不从已删除 worktree 复制旧 bridge。真实字段确认前不得写共享 `game_overlay_slots.v1.json`、不得接入 `ServiceManager`、不得修改 Python fallback。

## 6. 官方参考入口

- `https://dev.overwolf.com/ow-electron/getting-started/overview`
- `https://dev.overwolf.com/ow-electron/live-game-data-gep/live-game-data-gep-intro`
- `https://dev.overwolf.com/ow-electron/live-game-data-gep/game-events-status-health`

外部文档会变化，恢复路线时必须重新核验，不以本文中的版本号、game id 或 package 行为替代官方事实。
