# SMS 验证码多来源监控

实时轮询 LuDan SMS 接码平台、固定文本接码链接和邮箱取件接口，终端按来源显示号码/邮箱与最新验证码，并可在同一面板展示本机账户档案。

## 使用

1. 首次使用：复制 `config.example.json` 为 `config.json`，把 `key` 改成你的 CDK，并把 `fixed_sources` 里的 `url` 改成真实接码链接。
2. 双击 `run.bat` 启动（或命令行 `python monitor.py`）。
3. 面板会显示 LuDan 动态号码、未归属账户的固定号码/邮箱来源和账户档案；已被账户关联的接码来源会聚合到账户卡片内，不在顶层重复列出。美国号码会拆成国家码 `+1` 与 10 位号码，复制区域只放 10 位号码。
4. 按来源序号手动复制对应 10 位号码/邮箱；检测到新验证码时会自动复制验证码。账户档案按序号后会进入复制子菜单，2FA 动态码只在面板显示。
5. 启动后默认低频刷新，方便滚动查看历史；复制号码/邮箱/账户项或手动换号后，会进入高频轮询，拿到新验证码并自动复制后回到低频。

## 依赖

- Python 3 + `requests`（`python -m pip install requests`）。
- 剪贴板与热键功能依赖 Windows（`clip` / `msvcrt`）；其他系统仅 Ctrl+C 退出可用。

## 热键

- 数字键复制对应来源的 10 位号码/邮箱；账户来源会先打开复制子菜单
- `n` 仅 LuDan 换号
- `q` / Ctrl+C 退出

## 配置项

| 字段 | 说明 |
| --- | --- |
| `base_url` | 开放 API 地址 |
| `key` | 你的 CDK（接口认证 key） |
| `poll_interval` | 高频轮询间隔秒数（默认 5，限频 45 次/60 秒） |
| `idle_poll_interval` | 低频轮询间隔秒数（默认 15）；未复制前按此节奏刷新，避免终端滚轮被频繁清屏打断 |
| `active_until_code` | 复制或手动换号后是否一直高频轮询到拿到新验证码（默认 `true`）；拿到验证码后自动回低频 |
| `active_after_copy_seconds` | `active_until_code=false` 时的高频轮询秒数（默认 180） |
| `auto_change_on_expire` | 号码过期时是否自动换号 |
| `fixed_sources` | 固定文本接码链接数组；每项包含 `label`、`phone`、`url` |
| `email_sources` | 邮箱取件来源数组；每项含 `label`、`email`、`provider`（目前仅 `icloud`）、`base_url`（默认 `https://email.nloop.cc`）。走 `POST {base_url}/api/{provider}/query` 拉最新邮件并自动提取验证码 |
| `accounts` | 账户档案数组；每项含 `label`、`login_email`、`password`、`totp_secret`、`phone`、`email`。面板会用标准库实时计算 6 位 TOTP 并显示剩余秒数，`phone` / `email` 用来标注关联的接码来源 |

> `config.json` 含真实 key、接码链接 token、账户密码和 2FA 密钥，已被 `.gitignore` 忽略，不会提交。账户密码和 2FA 密钥属于明文存储，仅适合本机临时使用；`config.example.json` 只能放占位值。
