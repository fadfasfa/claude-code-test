#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多来源 SMS 接码验证码实时监控脚本。

职责：读取同目录 config.json，保留 LuDan 动态号码，同时轮询固定文本链接，
在终端中分块展示每个来源的美国号码与最新验证码。
调用方：用户双击 run.bat 或命令行 `python monitor.py` 运行。
关键依赖：requests（HTTP 请求）；Windows 自带 clip 命令（手动复制）；标准库 msvcrt（热键，可选）。
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

try:
    import requests
except ImportError:  # 缺少依赖时给中文提示
    print("未检测到 requests 库，请先运行：python -m pip install requests")
    sys.exit(1)

# Windows 下用 msvcrt 实现非阻塞热键；非 Windows 平台降级为仅 Ctrl+C 退出
try:
    import msvcrt
except ImportError:  # 非 Windows
    msvcrt = None

# 脚本所在目录，保证从任意工作目录运行都能找到配置文件
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")

DATE_TIME_RE = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
)
CODE_PATTERNS = [
    re.compile(
        r"(?:verification\s+code|security\s+code|login\s+code|code|otp)[^\d]{0,30}(\d{4,8})",
        re.IGNORECASE,
    ),
    re.compile(r"(?:验证码|校验码|动态码|登录码)[^\d]{0,30}(\d{4,8})"),
]
GENERIC_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


@dataclass(frozen=True)
class PhoneParts:
    """美国号码展示结构，避免把国家码和 10 位号码混在同一复制区域。"""

    country_code: str
    local_number: str
    raw_digits: str


@dataclass(frozen=True)
class FixedSmsParseResult:
    """固定文本接码链接解析结果。"""

    has_sms: bool
    code: str
    status: str
    content: str


def split_us_phone(phone):
    """把美国号码拆成 `+1` 与本地 10 位号码；异常格式保留可见数字。"""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        return PhoneParts(country_code="+1", local_number=digits[1:], raw_digits=digits)
    if len(digits) == 10:
        return PhoneParts(country_code="+1", local_number=digits, raw_digits=digits)
    return PhoneParts(country_code="", local_number=digits, raw_digits=digits)


def parse_fixed_sms_response(text):
    """解析固定接码链接的文本响应；先识别空状态，再剔除日期后提取验证码。"""
    raw = (text or "").strip()
    normalized = re.sub(r"\s+", " ", raw)
    lower = normalized.lower()
    if not normalized:
        return FixedSmsParseResult(False, "", "空响应", raw)

    if "暂无短信" in normalized:
        return FixedSmsParseResult(False, "", "暂无短信", raw)
    if lower.startswith("no sms"):
        return FixedSmsParseResult(False, "", "no sms", raw)

    without_dates = DATE_TIME_RE.sub(" ", normalized)
    for pattern in CODE_PATTERNS:
        match = pattern.search(without_dates)
        if match:
            return FixedSmsParseResult(True, match.group(1), "收到验证码", raw)

    match = GENERIC_CODE_RE.search(without_dates)
    if match:
        return FixedSmsParseResult(True, match.group(1), "收到验证码", raw)

    return FixedSmsParseResult(False, "", "未发现验证码", raw)


def load_config():
    """读取 config.json；缺失或仍为占位符时给中文提示并退出。"""
    if not os.path.exists(CONFIG_PATH):
        print("未找到配置文件 config.json。")
        print(f"请复制 {EXAMPLE_PATH}")
        print(f"     为 {CONFIG_PATH}，并把 key 改成你的 CDK。")
        sys.exit(1)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"读取 config.json 失败：{e}")
        sys.exit(1)

    key = (cfg.get("key") or "").strip()
    if not key or key == "YOUR_CDK":
        print("config.json 里的 key 还是占位符，请填入你的真实 CDK。")
        sys.exit(1)

    cfg["key"] = key
    cfg.setdefault("base_url", "https://jm.luudan.xyz/api/open.php")
    cfg.setdefault("poll_interval", 5)
    cfg.setdefault("auto_change_on_expire", True)
    cfg["fixed_sources"] = normalize_fixed_sources(cfg.get("fixed_sources", []))
    return cfg


def normalize_fixed_sources(raw_sources):
    """校验固定来源配置；错误信息不打印真实 URL，避免 token 泄漏到终端。"""
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        print("config.json 里的 fixed_sources 必须是数组。")
        sys.exit(1)

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            print(f"fixed_sources 第 {index} 项必须是对象。")
            sys.exit(1)
        label = str(item.get("label") or f"固定来源{index}").strip()
        phone = str(item.get("phone") or "").strip()
        url = str(item.get("url") or "").strip()
        if not phone or not url:
            print(f"fixed_sources 第 {index} 项缺少 phone 或 url。")
            sys.exit(1)
        sources.append({"label": label, "phone": phone, "url": url})
    return sources


def copy_to_clipboard(text):
    """把文本写入 Windows 系统剪贴板；失败则静默忽略，不影响主流程。"""
    try:
        proc = subprocess.Popen("clip", stdin=subprocess.PIPE, shell=True)
        proc.communicate(input=text.encode("utf-16-le"))
        return proc.returncode == 0
    except Exception:
        return False


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def now_hms():
    return datetime.now().strftime("%H:%M:%S")


def snippet(text, limit=46):
    """把短信内容压成单行预览，避免面板被长文本撑乱。"""
    compact = re.sub(r"\s+", " ", text or "").strip()
    return compact[:limit]


class LuDanSource:
    """LuDan 动态号码来源，保留原有 CDK、换号和过期处理逻辑。"""

    def __init__(self, cfg, session):
        self.label = "LuDan"
        self.base_url = cfg["base_url"]
        self.key = cfg["key"]
        self.auto_change = bool(cfg.get("auto_change_on_expire", True))
        self.session = session

        self.phone = ""
        self.last_code = ""
        self.history = deque(maxlen=8)
        self.card_status = "-"
        self.sms_count = 0
        self.switch_count = 0
        self.expires_in = None
        self.note = ""

    @property
    def phone_parts(self):
        return split_us_phone(self.phone)

    @property
    def copy_number(self):
        return self.phone_parts.local_number

    def verify(self):
        data = self.call("verify")
        if data.get("code") != 0:
            print(f"CDK 校验失败：{data.get('msg', '未知错误')}（code={data.get('code')}）")
            sys.exit(1)

    def call(self, action):
        """调用开放 API，返回解析后的 dict；网络/限频错误内部重试。"""
        params = {"action": action, "key": self.key}
        for _ in range(3):
            try:
                resp = self.session.get(self.base_url, params=params, timeout=10)
                if resp.status_code == 429:
                    self.note = "请求过于频繁，稍等重试..."
                    time.sleep(3)
                    continue
                return resp.json()
            except requests.RequestException as e:
                self.note = f"网络异常重试中（{e.__class__.__name__}）"
                time.sleep(2)
            except ValueError:
                self.note = "接口返回非 JSON，稍后重试"
                time.sleep(2)
        return {"code": -1, "msg": "本地请求失败"}

    def refresh_number(self):
        """优先用 status 拿当前绑定号码；没有号码则 get_number 分配。"""
        data = self.call("status")
        d = data.get("data", {}) if data.get("code") == 0 else {}
        phone = d.get("phone")
        self.card_status = d.get("card_status", self.card_status)
        if not phone:
            data = self.call("get_number")
            d = data.get("data", {})
            phone = d.get("phone")
            self.expires_in = d.get("expires_in")
        self._set_phone(phone)

    def change_number(self):
        data = self.call("change_number")
        if data.get("code") == 0:
            d = data.get("data", {})
            self._set_phone(d.get("phone"))
            self.expires_in = d.get("expires_in")
            self.switch_count = d.get("switch_count", self.switch_count)
            self.last_code = ""
            self.note = "已换号"
        else:
            self.note = f"换号失败：{data.get('msg', '')}"

    def poll(self):
        """轮询验证码；处理新验证码与号码过期。"""
        data = self.call("get_code")
        if data.get("code") != 0:
            self.note = data.get("msg", "查询失败")
            return
        d = data.get("data", {})
        self.sms_count = d.get("sms_count", self.sms_count)
        if "expires_in" in d:
            self.expires_in = d.get("expires_in")
        elif "remaining_seconds" in d:
            self.expires_in = d.get("remaining_seconds")

        if d.get("has_sms"):
            code = d.get("sms_code", "")
            content = d.get("content", "")
            if code and code != self.last_code:
                self.last_code = code
                self.history.appendleft((now_hms(), code, content))
                self.note = "收到新验证码"
                return code
        elif d.get("expired"):
            if self.auto_change:
                self.note = "号码过期，自动换号中..."
                self.change_number()
            else:
                self.note = "号码已过期，按 n 换号"
        return None

    def _set_phone(self, phone):
        """只更新号码，不自动写剪贴板；复制必须由数字热键触发。"""
        if phone and phone != self.phone:
            self.phone = phone

    def expire_text(self):
        if self.expires_in is None:
            return "-"
        try:
            secs = int(self.expires_in)
        except (TypeError, ValueError):
            return "-"
        if secs <= 0:
            return "已过期"
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def status_text(self):
        return (
            f"状态:{self.card_status} | 短信:{self.sms_count} 次 | "
            f"换号:{self.switch_count} 次 | 有效期:{self.expire_text()}"
        )


class FixedUrlSource:
    """固定文本 URL 来源，用本地解析规则提取最新验证码。"""

    def __init__(self, cfg, session):
        self.label = cfg["label"]
        self.phone = cfg["phone"]
        self.url = cfg["url"]
        self.session = session

        self.last_code = ""
        self.history = deque(maxlen=8)
        self.status = "等待中"
        self.http_status = "-"
        self.last_checked = "-"
        self.note = ""

    @property
    def phone_parts(self):
        return split_us_phone(self.phone)

    @property
    def copy_number(self):
        return self.phone_parts.local_number

    def poll(self):
        try:
            resp = self.session.get(self.url, timeout=10)
            self.http_status = str(resp.status_code)
            self.last_checked = now_hms()
            if resp.status_code == 429:
                self.status = "请求过于频繁"
                self.note = "稍后重试"
                return
            if not 200 <= resp.status_code < 300:
                self.status = f"HTTP {resp.status_code}"
                self.note = "查询失败"
                return
        except requests.RequestException as e:
            self.last_checked = now_hms()
            self.status = "网络异常"
            self.note = e.__class__.__name__
            return

        result = parse_fixed_sms_response(resp.text)
        self.status = result.status
        if result.has_sms and result.code != self.last_code:
            self.last_code = result.code
            self.history.appendleft((now_hms(), result.code, result.content))
            self.note = "收到新验证码"
            return result.code
        elif not result.has_sms:
            self.note = result.status
        return None

    def status_text(self):
        return f"状态:{self.status} | HTTP:{self.http_status} | 更新时间:{self.last_checked}"


class SmsMonitor:
    """封装一次监控会话的状态、渲染与热键处理。"""

    def __init__(self, cfg):
        self.poll_interval = max(2, int(cfg.get("poll_interval", 5)))
        self.session = requests.Session()
        self.ludan = LuDanSource(cfg, self.session)
        self.fixed_sources = [FixedUrlSource(item, self.session) for item in cfg["fixed_sources"]]
        self.sources = [self.ludan, *self.fixed_sources]
        self.note = ""

    def render(self):
        clear_screen()
        line = "=" * 68
        print(line)
        print("                    SMS 验证码多来源监控")
        print(line)
        print()
        for index, source in enumerate(self.sources, start=1):
            self.render_source(index, source)
            print()
        if msvcrt:
            keys = "/".join(str(i) for i in range(1, min(len(self.sources), 9) + 1))
            print(f"  热键：按 {keys} 复制对应 10 位号码 | n 仅 LuDan 换号 | q 退出")
        else:
            print("  非 Windows 终端仅支持 Ctrl+C 退出；号码请手动框选复制。")
        if self.note:
            print(f"  >> {self.note}")
        print(line)

    def render_source(self, index, source):
        parts = source.phone_parts
        local_number = parts.local_number or "(获取中)"
        border_width = max(18, len(local_number) + 4)
        border = "+" + "-" * border_width + "+"

        print(f"  [{index}] {source.label}")
        print(f"      国家码：{parts.country_code or '-'}")
        print("      可复制号码：")
        print(f"      {border}")
        print(f"      | {local_number:^{border_width - 2}} |")
        print(f"      {border}")
        print(f"      最新验证码：{source.last_code or '等待中...'}")
        print(f"      {source.status_text()}")
        if source.note:
            print(f"      提示：{source.note}")
        print("      最近收到：")
        if source.history:
            for ts, code, content in source.history:
                print(f"        [{ts}] {code}   {snippet(content)}")
        else:
            print("        （暂无）")

    def handle_keys(self):
        """非阻塞读取热键；返回 False 表示请求退出。"""
        if not msvcrt:
            return True
        while msvcrt.kbhit():
            ch = msvcrt.getwch().lower()
            if ch == "q":
                return False
            if ch == "n":
                self.note = "LuDan 手动换号中..."
                self.render()
                self.ludan.change_number()
                continue
            if ch.isdigit() and ch != "0":
                self.copy_source_number(int(ch))
        return True

    def copy_source_number(self, index):
        if index < 1 or index > len(self.sources):
            self.note = f"没有第 {index} 个号码来源"
            return
        source = self.sources[index - 1]
        number = source.copy_number
        if not number:
            self.note = f"[{index}] {source.label} 暂无可复制号码"
            return
        ok = copy_to_clipboard(number)
        self.note = f"已复制 [{index}] {source.label} 的 10 位号码" if ok else "复制失败"

    def auto_copy_code(self, label, code, copy_func=copy_to_clipboard):
        """新验证码自动写入剪贴板；号码复制仍只由数字热键触发。"""
        if not code:
            return False
        ok = copy_func(code)
        self.note = f"已自动复制 [{label}] 的验证码" if ok else f"[{label}] 验证码自动复制失败"
        return ok

    def run(self):
        print("正在校验 LuDan CDK...")
        self.ludan.verify()
        print("正在获取 LuDan 号码...")
        self.ludan.refresh_number()
        try:
            while True:
                for source in self.sources:
                    new_code = source.poll()
                    if new_code:
                        self.auto_copy_code(source.label, new_code)
                self.render()
                # 在 poll_interval 期间分片检查热键，保证按键响应灵敏
                waited = 0.0
                while waited < self.poll_interval:
                    if not self.handle_keys():
                        return
                    time.sleep(0.2)
                    waited += 0.2
        except KeyboardInterrupt:
            pass
        finally:
            print("\n已退出监控。")


def main():
    cfg = load_config()
    SmsMonitor(cfg).run()


if __name__ == "__main__":
    main()
