# LuDan SMS 验证码监控

实时轮询 LuDan SMS 接码平台，终端显示当前号码（自动复制到剪贴板）与最新验证码。

## 使用

1. 首次使用：复制 `config.example.json` 为 `config.json`，把 `key` 改成你的 CDK。
2. 双击 `run.bat` 启动（或命令行 `python monitor.py`）。
3. 号码会自动复制到剪贴板，直接 Ctrl+V 填到目标网站；几秒后收到的验证码会显示在面板上。

## 依赖

- Python 3 + `requests`（`python -m pip install requests`）。
- 剪贴板与热键功能依赖 Windows（`clip` / `msvcrt`）；其他系统仅 Ctrl+C 退出可用。

## 热键

- `n` 换号　`r` 重新复制号码　`q` / Ctrl+C 退出

## 配置项

| 字段 | 说明 |
| --- | --- |
| `base_url` | 开放 API 地址 |
| `key` | 你的 CDK（接口认证 key） |
| `poll_interval` | 轮询间隔秒数（默认 5，限频 45 次/60 秒） |
| `auto_change_on_expire` | 号码过期时是否自动换号 |

> `config.json` 含真实 key，已被 `.gitignore` 忽略，不会提交。
