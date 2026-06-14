#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多来源 SMS 接码验证码实时监控脚本。

职责：读取同目录 config.json，保留 LuDan 动态号码，同时轮询固定文本链接，
在终端中分块展示每个来源的美国号码与最新验证码。
调用方：用户双击 run.bat 或命令行 `python monitor.py` 运行。
关键依赖：requests（HTTP 请求）；Windows 自带 clip 命令（手动复制）；标准库 msvcrt（热键，可选）。
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
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


def generate_totp(secret, digits=6, period=30, timestamp=None):
    """用标准 RFC6238/HOTP 规则生成 TOTP；调用方负责保护原始密钥。"""
    normalized = re.sub(r"\s+", "", secret or "").upper()
    if not normalized:
        raise ValueError("TOTP 密钥为空")
    normalized += "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(normalized, casefold=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("TOTP 密钥不是合法 base32") from e

    now = time.time() if timestamp is None else timestamp
    counter = int(now // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code_int % (10 ** digits):0{digits}d}"


def totp_remaining(period=30):
    """返回当前 TOTP 周期剩余秒数；用于面板倒计时展示。"""
    return period - (int(time.time()) % period)


def parse_fixed_sms_response(text, allow_generic=True):
    """解析固定接码链接的文本响应；先识别空状态，再剔除日期后提取验证码。

    allow_generic=False 时跳过裸数字兜底，仅信关键字模式，避免 HTML 页面里的
    端口号/年份等无关数字被误判成验证码。
    """
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

    if allow_generic:
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
    cfg["email_sources"] = normalize_email_sources(cfg.get("email_sources", []))
    cfg["accounts"] = normalize_accounts(cfg.get("accounts", []))
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


def normalize_email_sources(raw_sources):
    """校验邮箱接码来源配置；错误信息不打印 email 全文，避免账户泄漏到终端。"""
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        print("config.json 里的 email_sources 必须是数组。")
        sys.exit(1)

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            print(f"email_sources 第 {index} 项必须是对象。")
            sys.exit(1)
        provider = str(item.get("provider") or "icloud").strip().lower()
        if provider != "icloud":
            print(f"email_sources 第 {index} 项的 provider={provider} 暂不支持，目前只支持 icloud。")
            sys.exit(1)
        label = str(item.get("label") or f"邮箱来源{index}").strip()
        email = str(item.get("email") or "").strip()
        base_url = str(item.get("base_url") or "https://email.nloop.cc").strip().rstrip("/")
        if not email:
            print(f"email_sources 第 {index} 项缺少 email。")
            sys.exit(1)
        sources.append(
            {"label": label, "email": email, "provider": provider, "base_url": base_url}
        )
    return sources


def normalize_accounts(raw_accounts):
    """校验账户档案配置；错误信息不回显密码或 2FA 密钥。"""
    if raw_accounts is None:
        return []
    if not isinstance(raw_accounts, list):
        print("config.json 里的 accounts 必须是数组。")
        sys.exit(1)

    accounts = []
    for index, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            print(f"accounts 第 {index} 项必须是对象。")
            sys.exit(1)
        login_email = str(item.get("login_email") or "").strip()
        if not login_email:
            print(f"accounts 第 {index} 项缺少 login_email。")
            sys.exit(1)
        label = str(item.get("label") or f"账户{index}").strip()
        accounts.append(
            {
                "label": label,
                "login_email": login_email,
                "password": str(item.get("password") or ""),
                "totp_secret": str(item.get("totp_secret") or ""),
                "phone": str(item.get("phone") or "").strip(),
                "email": str(item.get("email") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            }
        )
    return accounts


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
        self.verified_data = {}

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
        self.verified_data = data.get("data", {}) or {}
        self.apply_status_data(self.verified_data)
        return data

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
        """优先复用 verify 返回的已分配号码；没有号码才申请新号。"""
        d = self.verified_data or {}
        phone = d.get("phone")
        self.apply_status_data(d)
        if not phone:
            data = self.call("get_number")
            d = data.get("data", {})
            phone = d.get("phone")
            self.apply_status_data(d)
        self._set_phone(phone)

    def apply_status_data(self, data):
        """吸收 LuDan API 的状态字段；verify/get_number/get_code 返回结构略有差异。"""
        if not data:
            return
        self.card_status = data.get("card_status", self.card_status)
        self.sms_count = data.get("sms_count", self.sms_count)
        self.switch_count = data.get("switch_count", self.switch_count)
        if "expires_in" in data:
            self.expires_in = data.get("expires_in")
        elif "remaining_seconds" in data:
            self.expires_in = data.get("remaining_seconds")

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
        self.apply_status_data(d)

        if d.get("has_sms"):
            code = d.get("sms_code") or d.get("code", "")
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

        # HTML/XML 页面噪声多，禁用裸数字兜底，只信关键字模式
        content_type = resp.headers.get("Content-Type", "").lower()
        allow_generic = "html" not in content_type and "xml" not in content_type
        result = parse_fixed_sms_response(resp.text, allow_generic=allow_generic)
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


class EmailSource:
    """邮箱接码来源；POST email.nloop.cc 的取件接口，复用本地规则提取验证码。"""

    # 渲染时据此把"可复制号码"文案切换为"邮箱地址"
    is_email = True

    def __init__(self, cfg, session):
        self.label = cfg["label"]
        self.email = cfg["email"]
        self.provider = cfg["provider"]
        self.base_url = cfg["base_url"]
        self.session = session

        self.last_code = ""
        self.last_mail_id = ""
        self.history = deque(maxlen=8)
        self.status = "等待中"
        self.http_status = "-"
        self.last_checked = "-"
        self.note = ""

    @property
    def phone_parts(self):
        # 邮箱来源没有号码，借用 PhoneParts 让 render 兼容：邮箱地址放在本地号码位
        return PhoneParts(country_code="", local_number=self.email, raw_digits="")

    @property
    def copy_number(self):
        return self.email

    def poll(self):
        """取最新一封邮件并提取验证码；按邮件 id 去重，避免重复提示同一封。"""
        url = f"{self.base_url}/api/{self.provider}/query"
        try:
            resp = self.session.post(url, json={"email": self.email}, timeout=10)
            self.http_status = str(resp.status_code)
            self.last_checked = now_hms()
            if resp.status_code == 429:
                self.status = "请求过于频繁"
                self.note = "稍后重试"
                return
            if not 200 <= resp.status_code < 300:
                self.status = f"HTTP {resp.status_code}"
                self.note = "取件失败"
                return
            payload = resp.json()
        except requests.RequestException as e:
            self.last_checked = now_hms()
            self.status = "网络异常"
            self.note = e.__class__.__name__
            return
        except ValueError:
            self.last_checked = now_hms()
            self.status = "返回非 JSON"
            self.note = "稍后重试"
            return

        if not payload.get("ok"):
            self.status = "取件失败"
            self.note = str(payload.get("error") or payload.get("detail") or "接口返回 ok=false")
            return

        mails = payload.get("mails") or []
        if not mails:
            self.status = "暂无邮件"
            return

        mail = mails[0]  # latest 模式最新一封在最前
        self.status = "已取件"
        mail_id = str(mail.get("id") or "")
        subject = mail.get("subject") or ""
        body = mail.get("body") or mail.get("preview") or ""
        result = parse_fixed_sms_response(f"{subject} {body}", allow_generic=True)
        if result.has_sms and (result.code != self.last_code or mail_id != self.last_mail_id):
            self.last_code = result.code
            self.last_mail_id = mail_id
            self.history.appendleft((now_hms(), result.code, subject or result.content))
            self.note = "收到新验证码"
            return result.code
        if not result.has_sms:
            self.last_mail_id = mail_id
            self.note = "最新邮件未发现验证码"
        return None

    def status_text(self):
        return f"状态:{self.status} | HTTP:{self.http_status} | 更新时间:{self.last_checked}"


class AccountSource:
    """账户档案来源；只展示和手动复制静态项，不参与验证码自动复制。"""

    is_account = True

    def __init__(self, cfg):
        self.label = cfg["label"]
        self.login_email = cfg["login_email"]
        self.password = cfg.get("password", "")
        self.totp_secret = cfg.get("totp_secret", "")
        self.phone = cfg.get("phone", "")
        self.email = cfg.get("email", "")
        self.note = cfg.get("note", "")
        # 聚合卡片用：由 SmsMonitor._link_accounts 挂上对应的轮询来源
        self.linked_phone_source: "FixedUrlSource | None" = None
        self.linked_email_source: "EmailSource | None" = None

    @property
    def current_totp(self):
        if not self.totp_secret:
            return ""
        try:
            return generate_totp(self.totp_secret)
        except ValueError:
            return "密钥无效"

    @property
    def phone_parts(self):
        # 账户没有可轮询号码，借用可复制框的语义把登录邮箱作为标识。
        return PhoneParts(country_code="", local_number=self.login_email, raw_digits="")

    @property
    def copy_number(self):
        return self.login_email

    def copy_fields(self):
        fields = []
        candidates = [
            ("登录邮箱", self.login_email),
            ("密码", self.password),
            ("关联电话", split_us_phone(self.phone).local_number),
        ]
        for label, value in candidates:
            if value:
                fields.append((str(len(fields) + 1), label, value))
        return fields

    def poll(self):
        return None

    def status_text(self):
        return f"登录:{self.login_email} | 2FA:{'有' if self.totp_secret else '无'}"


class SmsMonitor:
    """封装一次监控会话的状态、渲染与热键处理。"""

    def __init__(self, cfg):
        self.poll_interval = max(2, int(cfg.get("poll_interval", 5)))
        self.idle_poll_interval = max(2, int(cfg.get("idle_poll_interval", 15)))
        self.active_after_copy_seconds = max(0, int(cfg.get("active_after_copy_seconds", 180)))
        self.active_until_code = bool(cfg.get("active_until_code", True))
        self.active_waiting_for_code = False
        self.active_until = 0.0
        self.session = requests.Session()
        self.ludan = LuDanSource(cfg, self.session)
        self.fixed_sources = [FixedUrlSource(item, self.session) for item in cfg["fixed_sources"]]
        self.email_sources = [EmailSource(item, self.session) for item in cfg.get("email_sources", [])]
        self.accounts = [AccountSource(item) for item in cfg.get("accounts", [])]
        # 需要后台轮询的验证码来源（账户本身不轮询）
        self.pollables = [self.ludan, *self.fixed_sources, *self.email_sources]
        # 账户聚合：把电话/邮箱来源挂到账户卡上，避免顶层重复展示
        self._link_accounts()
        linked_ids = set()
        for account in self.accounts:
            if account.linked_phone_source is not None:
                linked_ids.add(id(account.linked_phone_source))
            if account.linked_email_source is not None:
                linked_ids.add(id(account.linked_email_source))
        # 顶层显示与热键序号：未被账户引用的来源 + 账户卡片
        unlinked = [s for s in self.pollables if id(s) not in linked_ids]
        self.sources = [*unlinked, *self.accounts]
        self.note = ""

    def is_active_mode(self, now=None):
        now = time.time() if now is None else now
        return self.active_waiting_for_code or now < self.active_until

    def activate_high_frequency(self, now=None):
        now = time.time() if now is None else now
        if self.active_until_code:
            self.active_waiting_for_code = True
            return
        self.active_until = max(self.active_until, now + self.active_after_copy_seconds)

    def deactivate_high_frequency(self):
        self.active_waiting_for_code = False
        self.active_until = 0.0

    def current_poll_interval(self, now=None):
        return self.poll_interval if self.is_active_mode(now) else self.idle_poll_interval

    def _link_accounts(self):
        """按电话(去格式比对)与取件邮箱，把轮询来源挂到账户上供聚合卡片展示。"""
        for account in self.accounts:
            account.linked_phone_source = None
            account.linked_email_source = None
            if account.phone:
                target = split_us_phone(account.phone).raw_digits
                for src in self.fixed_sources:
                    if target and split_us_phone(src.phone).raw_digits == target:
                        account.linked_phone_source = src
                        break
            if account.email:
                target = account.email.strip().lower()
                for src in self.email_sources:
                    if src.email.strip().lower() == target:
                        account.linked_email_source = src
                        break

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
            print(f"  热键：按 {keys} 复制对应号码/邮箱 | n 仅 LuDan 换号 | q 退出")
        else:
            print("  非 Windows 终端仅支持 Ctrl+C 退出；号码请手动框选复制。")
        if self.note:
            print(f"  >> {self.note}")
        print(line)

    def render_source(self, index, source):
        if getattr(source, "is_account", False):
            self.render_account_source(index, source)
            return

        parts = source.phone_parts
        is_email = getattr(source, "is_email", False)
        local_number = parts.local_number or ("(获取中)" if not is_email else "(未配置)")
        border_width = max(18, len(local_number) + 4)
        border = "+" + "-" * border_width + "+"

        print(f"  [{index}] {source.label}")
        if is_email:
            print("      可复制邮箱地址：")
        else:
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

    def render_account_source(self, index, source):
        print(f"  [{index}] {source.label}（账户）")
        print(f"      登录邮箱：{source.login_email}")
        print(f"      密码：{source.password or '未配置'}")
        if source.totp_secret:
            current = source.current_totp
            if current == "密钥无效":
                print("      2FA 动态码：密钥无效")
            else:
                print(f"      2FA 动态码：{current}（剩余 {totp_remaining()} 秒）")
        else:
            print("      2FA 动态码：未配置")

        # 短信渠道：聚合显示关联电话来源的号码与最新验证码
        phone_src = source.linked_phone_source
        if phone_src is not None:
            parts = phone_src.phone_parts
            print(f"      短信号码：{parts.country_code or '-'} {parts.local_number}（{phone_src.label}）")
            print(f"        最新验证码：{phone_src.last_code or '等待中...'} | {phone_src.status_text()}")
        elif source.phone:
            parts = split_us_phone(source.phone)
            print(f"      短信号码：{parts.country_code or '-'} {parts.local_number}（无轮询来源）")

        # 取件邮箱渠道：聚合显示关联邮箱来源的最新验证码
        email_src = source.linked_email_source
        if email_src is not None:
            print(f"      取件邮箱：{email_src.email}（{email_src.label}）")
            print(f"        最新验证码：{email_src.last_code or '等待中...'} | {email_src.status_text()}")
        elif source.email:
            print(f"      取件邮箱：{source.email}（无轮询来源）")

        copy_labels = " / ".join(label for _, label, _ in source.copy_fields()) or "无"
        print(f"      可复制项：按 {index} 选择 {copy_labels}")
        if source.note:
            print(f"      提示：{source.note}")

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
                self.activate_high_frequency()
                self.render()  # 立即刷新换号结果，不等下一轮轮询
                continue
            if ch.isdigit() and ch != "0":
                self.copy_source_number(int(ch))
        return True

    def copy_source_number(self, index):
        if index < 1 or index > len(self.sources):
            self.note = f"没有第 {index} 个号码来源"
            return
        source = self.sources[index - 1]
        if getattr(source, "is_account", False):
            self.copy_account_field(index, source)
            return
        number = source.copy_number
        if not number:
            self.note = f"[{index}] {source.label} 暂无可复制号码"
            return
        ok = copy_to_clipboard(number)
        target = "邮箱地址" if getattr(source, "is_email", False) else "10 位号码"
        if ok:
            self.activate_high_frequency()
        self.note = f"已复制 [{index}] {source.label} 的{target}" if ok else "复制失败"

    def render_copy_menu(self, index, account, fields):
        clear_screen()
        print("=" * 68)
        print(f"  [{index}] {account.label} 账户复制项")
        print("=" * 68)
        for key, label, value in fields:
            visible = value if label != "密码" else "<密码>"
            print(f"  {key}. {label}：{visible}")
        print()
        print("  按对应数字复制；Esc 取消。")

    def copy_account_field(self, index, account):
        fields = account.copy_fields()
        if not fields:
            self.note = f"[{index}] {account.label} 暂无可复制项"
            return
        if not msvcrt:
            _, label, value = fields[0]
            ok = copy_to_clipboard(value)
            if ok:
                self.activate_high_frequency()
            self.note = (
                f"非 Windows 终端已复制 [{index}] {account.label} 的{label}"
                if ok
                else "复制失败"
            )
            return

        self.render_copy_menu(index, account, fields)
        choices = {key: (label, value) for key, label, value in fields}
        while True:
            ch = msvcrt.getwch().lower()
            if ch == "\x1b":
                self.note = f"已取消 [{index}] {account.label} 复制"
                self.render()
                return
            if ch in choices:
                label, value = choices[ch]
                ok = copy_to_clipboard(value)
                if ok:
                    self.activate_high_frequency()
                self.note = f"已复制 [{index}] {account.label} 的{label}" if ok else "复制失败"
                self.render()
                return

    def auto_copy_code(self, label, code, copy_func=copy_to_clipboard):
        """新验证码自动写入剪贴板；号码复制仍只由数字热键触发。"""
        if not code:
            return False
        ok = copy_func(code)
        self.note = f"已自动复制 [{label}] 的验证码" if ok else f"[{label}] 验证码自动复制失败"
        return ok

    def auto_copy_codes(self, pairs, copy_func=copy_to_clipboard):
        """本轮多个来源同时出新码时，复制第一个并在 note 列全部，避免静默覆盖。"""
        if not pairs:
            return
        first_label, first_code = pairs[0]
        self.auto_copy_code(first_label, first_code, copy_func)
        if len(pairs) > 1:
            extra = "；".join(f"[{label}] {code}" for label, code in pairs[1:])
            self.note += f"；另有 {extra} 同时收到，请手动取用"
        self.deactivate_high_frequency()

    def run(self):
        print("正在校验 LuDan CDK...")
        self.ludan.verify()
        print("正在获取 LuDan 号码...")
        self.ludan.refresh_number()
        try:
            while True:
                new_codes = []
                for source in self.pollables:
                    new_code = source.poll()
                    if new_code:
                        new_codes.append((source.label, new_code))
                self.auto_copy_codes(new_codes)
                self.render()
                # 低频模式保留终端滚动可用；复制/换号后切高频，才每秒刷新 TOTP。
                wait_interval = self.current_poll_interval()
                waited = 0.0
                last_render_second = int(time.time())
                while waited < wait_interval:
                    if not self.handle_keys():
                        return
                    if self.current_poll_interval() != wait_interval:
                        break
                    time.sleep(0.2)
                    waited += 0.2
                    current_second = int(time.time())
                    if self.is_active_mode() and self.accounts and current_second != last_render_second:
                        self.render()
                        last_render_second = current_second
        except KeyboardInterrupt:
            pass
        finally:
            print("\n已退出监控。")


def main():
    cfg = load_config()
    SmsMonitor(cfg).run()


if __name__ == "__main__":
    main()
