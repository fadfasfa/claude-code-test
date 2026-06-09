# SMS 验证码多来源监控

实时轮询 LuDan SMS 接码平台和固定文本接码链接，终端按来源显示号码与最新验证码。

## 使用

1. 首次使用：复制 `config.example.json` 为 `config.json`，把 `key` 改成你的 CDK，并把 `fixed_sources` 里的 `url` 改成真实接码链接。
2. 双击 `run.bat` 启动（或命令行 `python monitor.py`）。
3. 面板会显示 LuDan 动态号码和固定号码来源。美国号码会拆成国家码 `+1` 与 10 位号码，复制区域只放 10 位号码。
4. 按来源序号 `1` / `2` / `3` 手动复制对应 10 位号码；检测到新验证码时会自动复制验证码。

## 依赖

- Python 3 + `requests`（`python -m pip install requests`）。
- 剪贴板与热键功能依赖 Windows（`clip` / `msvcrt`）；其他系统仅 Ctrl+C 退出可用。

## 热键

- `1` / `2` / `3` 复制对应来源的 10 位号码
- `n` 仅 LuDan 换号
- `q` / Ctrl+C 退出

## 配置项

| 字段 | 说明 |
| --- | --- |
| `base_url` | 开放 API 地址 |
| `key` | 你的 CDK（接口认证 key） |
| `poll_interval` | 轮询间隔秒数（默认 5，限频 45 次/60 秒） |
| `auto_change_on_expire` | 号码过期时是否自动换号 |
| `fixed_sources` | 固定文本接码链接数组；每项包含 `label`、`phone`、`url` |

> `config.json` 含真实 key 和接码链接 token，已被 `.gitignore` 忽略，不会提交。`config.example.json` 只能放占位链接。
