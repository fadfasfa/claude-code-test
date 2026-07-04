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
import argparse
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from getpass import getpass

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
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"']+")


class ConfigCommandError(Exception):
    """配置命令的可诊断错误；消息不得包含真实 secret。"""


class LuDanAuthError(Exception):
    """LuDan CDK 校验失败；上层捕获后降级跳过 LuDan，不退出进程。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.message = message
        self.code = code


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


def looks_like_html(text):
    """内容是否像 HTML/XML（含标签）。

    yuntl 等平台把纯文本响应错标成 text/html，仅看 Content-Type 会错误禁用
    裸数字兜底，导致 Google 那种"数字在前、关键字在后"的验证码提取不到；
    这里只认真实标签，纯文本即使被标成 html 也按纯文本处理。
    """
    return bool(re.search(r"</?[a-zA-Z!?][^>]*>", text or ""))


def mask_email(value):
    if not value or "@" not in value:
        return ""
    name, domain = value.split("@", 1)
    return f"{name[:2]}***@{domain}"


def mask_tail(value, keep=4):
    text = str(value or "")
    if not text:
        return ""
    return f"***{text[-keep:]}" if len(text) > keep else "***"


def mask_url(value):
    if not value:
        return ""
    match = re.match(r"(https?://[^/?#]+)", value)
    host = match.group(1) if match else "url"
    return f"{host}/...<hidden>"


def split_freeform_chunks(text):
    """按常见分隔符拆自由文本；只用于本地解析，不输出原文。"""
    compact = re.sub(r"\s+", " ", text or "").strip()
    chunks = [part.strip(" -\t\r\n") for part in re.split(r"-{3,}|[|，,；;]+", compact)]
    return [part for part in chunks if part]


def parse_freeform_account_text(text, label):
    """从本地粘贴的非标准账号行中提取字段，并返回脱敏预览。"""
    raw = text or ""
    url_match = URL_RE.search(raw)
    sms_url = url_match.group(0).rstrip("。).,，；;") if url_match else ""
    without_url = raw.replace(url_match.group(0), " ") if url_match else raw

    email_match = EMAIL_RE.search(without_url)
    login_email = email_match.group(0) if email_match else ""
    without_email = without_url.replace(login_email, " ") if login_email else without_url

    phone = ""
    for candidate in re.findall(r"\+?\d[\d\s().-]{8,}\d", without_email):
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 15:
            phone = digits
    without_phone = without_email
    if phone:
        without_phone = re.sub(r"\+?\d[\d\s().-]{8,}\d", " ", without_phone)

    chunks = split_freeform_chunks(without_phone)
    password = chunks[0] if chunks else ""
    totp_secret = chunks[1] if len(chunks) > 1 else ""

    preview = {
        "label": label,
        "login_email": mask_email(login_email),
        "password": "<hidden>" if password else "",
        "totp_secret": "<hidden>" if totp_secret else "",
        "phone": mask_tail(phone),
        "sms_url": mask_url(sms_url),
    }
    missing = [
        name
        for name, value in {
            "login_email": login_email,
            "password": password,
            "totp_secret": totp_secret,
            "phone": phone,
            "sms_url": sms_url,
        }.items()
        if not value
    ]
    return {
        "label": label,
        "login_email": login_email,
        "password": password,
        "totp_secret": totp_secret,
        "phone": phone,
        "sms_url": sms_url,
        "preview": preview,
        "missing": missing,
    }


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


def default_config():
    """生成可被录入命令填充的空配置；不放真实凭据。"""
    return {
        "base_url": "https://jm.luudan.xyz/api/open.php",
        "key": "",
        "poll_interval": 5,
        "idle_poll_interval": 15,
        "request_timeout": 3,
        "max_poll_workers": 4,
        "poll_round_timeout": 3.5,
        "active_until_code": True,
        "active_after_copy_seconds": 180,
        "auto_change_on_expire": True,
        "fixed_sources": [],
        "email_sources": [],
        "accounts": [],
    }


def read_config_file(path, allow_missing=False):
    """读取配置命令使用的 JSON；不做真实 key 占位检查。"""
    if not os.path.exists(path):
        if allow_missing:
            return default_config()
        raise ConfigCommandError("配置文件不存在")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigCommandError(f"配置文件不可读：{e.__class__.__name__}") from e
    if not isinstance(cfg, dict):
        raise ConfigCommandError("配置文件根节点必须是对象")
    merged = default_config()
    merged.update(cfg)
    return merged


def write_config_file_atomic(path, cfg):
    """用同目录临时文件原子替换，避免留下长期备份副本。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(os.path.abspath(path)),
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def env_value(env, name, field_label):
    """从环境变量读取敏感值；错误信息只包含变量名，不回显值。"""
    if not name:
        return None
    if name not in env or env[name] == "":
        raise ConfigCommandError(f"{field_label} 环境变量未设置：{name}")
    return env[name]


def read_clipboard_text():
    """读取本机剪贴板；内容只在内存中用于解析，不打印。"""
    if os.name != "nt":
        raise ConfigCommandError("当前平台暂不支持 --from-clipboard，请使用 --stdin")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ConfigCommandError(f"读取剪贴板失败：{e.__class__.__name__}") from e
    if proc.returncode != 0:
        raise ConfigCommandError("读取剪贴板失败")
    return proc.stdout


def default_prompt_reader(prompt, secret=False):
    return getpass(prompt) if secret else input(prompt)


def fill_freeform_missing_fields(parsed, prompt_reader):
    """在本机终端补齐缺失字段；返回结构仍只用脱敏预览对外输出。"""
    prompts = {
        "login_email": ("登录邮箱：", False),
        "password": ("密码：", True),
        "totp_secret": ("2FA/TOTP secret：", True),
        "phone": ("手机号：", False),
        "sms_url": ("接码 URL：", True),
    }
    for field in list(parsed["missing"]):
        prompt, secret = prompts[field]
        value = prompt_reader(prompt, secret=secret).strip()
        parsed[field] = value
    parsed["phone"] = re.sub(r"\D", "", parsed.get("phone") or "")
    parsed["preview"] = {
        "label": parsed["label"],
        "login_email": mask_email(parsed.get("login_email")),
        "password": "<hidden>" if parsed.get("password") else "",
        "totp_secret": "<hidden>" if parsed.get("totp_secret") else "",
        "phone": mask_tail(parsed.get("phone")),
        "sms_url": mask_url(parsed.get("sms_url")),
    }
    parsed["missing"] = [
        field for field in prompts if not parsed.get(field)
    ]
    return parsed


def parse_bool(value):
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("必须是 true 或 false")


def safe_result(label, kind, ready, status, sanitized_reason):
    return {
        "label": label,
        "kind": kind,
        "ready": bool(ready),
        "status": status,
        "sanitized_reason": sanitized_reason,
    }


def upsert_by_label(items, item):
    """按 label 幂等更新数组项，避免 agent 重复录入时产生多条记录。"""
    label = item["label"]
    for index, existing in enumerate(items):
        if str(existing.get("label") or "") == label:
            items[index] = item
            return "updated"
    items.append(item)
    return "created"


# 无效来源/账户的持久化标记：顶层 disabled 字典，key 由 disabled_key 生成。
# 用顶层字典而非来源对象内字段，避免 normalize_* 重建 dict 时把标记剥掉。
def disabled_key(kind, label):
    """disabled 字典统一 key；LuDan 无 label 用 'ludan'，其余 '<kind>:<label>'。"""
    if kind == "ludan":
        return "ludan"
    return f"{kind}:{label}"


def is_disabled(cfg, kind, label):
    """某来源/账户是否已被标记无效。"""
    return disabled_key(kind, label) in (cfg.get("disabled") or {})


def disable_source(cfg, kind, label, reason, path=CONFIG_PATH):
    """标记某来源/账户为无效并持久化到 config.json。"""
    entry = cfg.setdefault("disabled", {})
    entry[disabled_key(kind, label)] = {"reason": reason, "at": now_hms()}
    write_config_file_atomic(path, cfg)


def enable_source(cfg, kind, label, path=CONFIG_PATH):
    """移除无效标记并持久化；返回是否实际移除。"""
    entry = cfg.get("disabled") or {}
    key = disabled_key(kind, label)
    if key not in entry:
        return False
    entry.pop(key)
    cfg["disabled"] = entry
    write_config_file_atomic(path, cfg)
    return True


def list_disabled(cfg):
    """返回 [(kind, label, reason, at)] 列表用于报告；LuDan 的 label 固定 'LuDan'。"""
    items = []
    for key, info in (cfg.get("disabled") or {}).items():
        if key == "ludan":
            items.append(("ludan", "LuDan", info.get("reason", ""), info.get("at", "")))
        else:
            kind, _, label = key.partition(":")
            items.append((kind, label or "未知", info.get("reason", ""), info.get("at", "")))
    return items


def positive_seconds(value):
    if value is None:
        return True
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return True


def has_usable_phone(data):
    if not data or not data.get("phone"):
        return False
    if data.get("expired"):
        return False
    return positive_seconds(data.get("expires_in", data.get("remaining_seconds")))


def ready_check_ludan(cfg, session):
    """校验 LuDan 是否已到可等待验证码状态，不要求已有验证码。"""
    source = LuDanSource(cfg, session, float(cfg.get("request_timeout", 3.0)))
    data = source.call("verify")
    if data.get("code") != 0:
        return safe_result("LuDan", "ludan", False, "auth_failed", "LuDan 校验失败")
    verified = data.get("data", {}) or {}
    if has_usable_phone(verified):
        return safe_result("LuDan", "ludan", True, "ready", "已有可用号码")

    number_data = source.call("get_number")
    if number_data.get("code") != 0:
        return safe_result("LuDan", "ludan", False, "number_unavailable", "无法获取可用号码")
    if has_usable_phone(number_data.get("data", {}) or {}):
        return safe_result("LuDan", "ludan", True, "ready", "已获取可用号码")
    return safe_result("LuDan", "ludan", False, "number_unavailable", "未返回可用号码")


def ready_check_fixed_source(cfg, session, request_timeout=3.0):
    """固定短信链接可达即可 ready；不要求已有验证码。"""
    label = cfg.get("label") or "fixed"
    try:
        resp = session.get(cfg["url"], timeout=request_timeout)
    except requests.RequestException as e:
        return safe_result(label, "fixed", False, "network_error", e.__class__.__name__)

    if not 200 <= resp.status_code < 300:
        return safe_result(label, "fixed", False, "http_error", f"HTTP {resp.status_code}")

    allow_generic = not looks_like_html(resp.text)
    result = parse_fixed_sms_response(resp.text, allow_generic=allow_generic)
    return safe_result(label, "fixed", True, "ready", result.status)


def ready_check_email_source(cfg, session, request_timeout=3.0):
    """邮箱取件接口 ok=true 即 ready；不要求已有验证码邮件。"""
    label = cfg.get("label") or "email"
    url = f"{cfg['base_url'].rstrip('/')}/api/{cfg['provider']}/query"
    try:
        resp = session.post(url, json={"email": cfg["email"]}, timeout=request_timeout)
    except requests.RequestException as e:
        return safe_result(label, "email", False, "network_error", e.__class__.__name__)

    if not 200 <= resp.status_code < 300:
        return safe_result(label, "email", False, "http_error", f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except (AttributeError, ValueError):
        return safe_result(label, "email", False, "bad_response", "接口返回非 JSON")
    if not payload.get("ok"):
        return safe_result(label, "email", False, "api_not_ready", "接口返回 ok=false")
    return safe_result(label, "email", True, "ready", "取件接口可用")


def same_phone(left, right):
    return bool(left and right and split_us_phone(left).raw_digits == split_us_phone(right).raw_digits)


def ready_check_account(account, fixed_sources, email_sources):
    """账户本地字段可用且已配置的接码关联能匹配时为 ready。"""
    label = account.get("label") or "account"
    if account.get("totp_secret"):
        try:
            generate_totp(account["totp_secret"])
        except ValueError:
            return safe_result(label, "account", False, "bad_totp", "TOTP 密钥无效")

    if account.get("phone") and not any(same_phone(account["phone"], src.get("phone")) for src in fixed_sources):
        return safe_result(label, "account", False, "phone_unlinked", "关联电话未匹配接码来源")
    if account.get("email"):
        target = account["email"].strip().lower()
        if not any((src.get("email") or "").strip().lower() == target for src in email_sources):
            return safe_result(label, "account", False, "email_unlinked", "关联邮箱未匹配取件来源")
    return safe_result(label, "account", True, "ready", "账户字段和关联可用")


def validate_config_result(cfg):
    """只验证结构和必要字段，不触发真实网络。"""
    normalize_fixed_sources(cfg.get("fixed_sources", []))
    normalize_email_sources(cfg.get("email_sources", []))
    normalize_accounts(cfg.get("accounts", []))
    if not str(cfg.get("key") or "").strip():
        return safe_result("config", "config", False, "missing_key", "LuDan key 未配置")
    return safe_result("config", "config", True, "valid", "配置结构可用")


def aggregate_results(results):
    ready = all(item["ready"] for item in results)
    return {
        "label": "all",
        "kind": "summary",
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "sanitized_reason": "全部来源可预备接码" if ready else "存在未就绪来源",
        "items": results,
    }


def ready_check_all(cfg, session_factory):
    """检查所有来源是否到达预备接码状态。"""
    request_timeout = max(0.5, float(cfg.get("request_timeout", 3.0)))
    fixed_sources = normalize_fixed_sources(cfg.get("fixed_sources", []))
    email_sources = normalize_email_sources(cfg.get("email_sources", []))
    accounts = normalize_accounts(cfg.get("accounts", []))

    results = [ready_check_ludan(cfg, session_factory())]
    for source in fixed_sources:
        results.append(ready_check_fixed_source(source, session_factory(), request_timeout))
    for source in email_sources:
        results.append(ready_check_email_source(source, session_factory(), request_timeout))
    for account in accounts:
        results.append(ready_check_account(account, fixed_sources, email_sources))
    return aggregate_results(results)


def build_config_parser():
    parser = argparse.ArgumentParser(prog="monitor.py config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--config", default=CONFIG_PATH)
        sub.add_argument("--json", action="store_true")

    init_parser = subparsers.add_parser("init")
    add_common(init_parser)

    global_parser = subparsers.add_parser("set-global")
    add_common(global_parser)
    global_parser.add_argument("--base-url")
    global_parser.add_argument("--key-env")
    global_parser.add_argument("--poll-interval", type=int)
    global_parser.add_argument("--idle-poll-interval", type=int)
    global_parser.add_argument("--request-timeout", type=float)
    global_parser.add_argument("--max-poll-workers", type=int)
    global_parser.add_argument("--poll-round-timeout", type=float)
    global_parser.add_argument("--active-until-code", type=parse_bool)
    global_parser.add_argument("--active-after-copy-seconds", type=int)
    global_parser.add_argument("--auto-change-on-expire", type=parse_bool)

    fixed_parser = subparsers.add_parser("upsert-fixed")
    add_common(fixed_parser)
    fixed_parser.add_argument("--label", required=True)
    fixed_parser.add_argument("--phone", required=True)
    fixed_parser.add_argument("--url-env", required=True)

    email_parser = subparsers.add_parser("upsert-email")
    add_common(email_parser)
    email_parser.add_argument("--label", required=True)
    email_parser.add_argument("--email", required=True)
    email_parser.add_argument("--provider", default="icloud")
    email_parser.add_argument("--base-url", default="https://email.nloop.cc")

    account_parser = subparsers.add_parser("upsert-account")
    add_common(account_parser)
    account_parser.add_argument("--label", required=True)
    account_parser.add_argument("--login-email", required=True)
    account_parser.add_argument("--password-env")
    account_parser.add_argument("--totp-secret-env")
    account_parser.add_argument("--phone", default="")
    account_parser.add_argument("--email", default="")
    account_parser.add_argument("--note", default="")

    import_parser = subparsers.add_parser("import-freeform")
    add_common(import_parser)
    import_parser.add_argument("--label", required=True)
    import_parser.add_argument("--source-label")
    import_parser.add_argument("--stdin", action="store_true")
    import_parser.add_argument("--from-clipboard", action="store_true")
    import_parser.add_argument("--interactive", action="store_true")
    import_parser.add_argument("--yes", action="store_true")

    validate_parser = subparsers.add_parser("validate")
    add_common(validate_parser)

    ready_parser = subparsers.add_parser("ready-check")
    add_common(ready_parser)
    ready_parser.add_argument("--all", action="store_true")

    disable_parser = subparsers.add_parser("disable")
    add_common(disable_parser)
    disable_parser.add_argument("--label", required=True)
    disable_parser.add_argument(
        "--kind", required=True, choices=["ludan", "fixed", "email", "account"]
    )
    disable_parser.add_argument("--reason", default="手动禁用")

    enable_parser = subparsers.add_parser("enable")
    add_common(enable_parser)
    enable_parser.add_argument("--label", required=True)
    enable_parser.add_argument(
        "--kind", required=True, choices=["ludan", "fixed", "email", "account"]
    )

    list_disabled_parser = subparsers.add_parser("list-disabled")
    add_common(list_disabled_parser)

    prune_parser = subparsers.add_parser("prune")
    add_common(prune_parser)
    prune_parser.add_argument("--yes", action="store_true")
    return parser


def run_config_command(
    argv,
    env=None,
    session_factory=None,
    input_reader=None,
    clipboard_reader=None,
    prompt_reader=None,
):
    """执行 config 子命令并返回脱敏结果；测试可注入 env/session。"""
    env = os.environ if env is None else env
    parser = build_config_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        cfg = read_config_file(args.config, allow_missing=True)
        write_config_file_atomic(args.config, cfg)
        return safe_result("config", "config", True, "initialized", "配置文件已准备")

    if args.command == "set-global":
        cfg = read_config_file(args.config, allow_missing=True)
        updates = {
            "base_url": args.base_url,
            "poll_interval": args.poll_interval,
            "idle_poll_interval": args.idle_poll_interval,
            "request_timeout": args.request_timeout,
            "max_poll_workers": args.max_poll_workers,
            "poll_round_timeout": args.poll_round_timeout,
            "active_until_code": args.active_until_code,
            "active_after_copy_seconds": args.active_after_copy_seconds,
            "auto_change_on_expire": args.auto_change_on_expire,
        }
        for key, value in updates.items():
            if value is not None:
                cfg[key] = value
        key_value = env_value(env, args.key_env, "LuDan key")
        if key_value is not None:
            cfg["key"] = key_value
        write_config_file_atomic(args.config, cfg)
        return safe_result("global", "config", True, "updated", "全局配置已更新")

    if args.command == "upsert-fixed":
        cfg = read_config_file(args.config, allow_missing=True)
        cfg["fixed_sources"] = normalize_fixed_sources(cfg.get("fixed_sources", []))
        url = env_value(env, args.url_env, "固定来源 URL")
        status = upsert_by_label(
            cfg["fixed_sources"],
            {"label": args.label.strip(), "phone": args.phone.strip(), "url": url},
        )
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "fixed", True, status, "固定短信来源已录入")

    if args.command == "upsert-email":
        cfg = read_config_file(args.config, allow_missing=True)
        cfg["email_sources"] = normalize_email_sources(cfg.get("email_sources", []))
        item = {
            "label": args.label.strip(),
            "email": args.email.strip(),
            "provider": args.provider.strip().lower(),
            "base_url": args.base_url.strip().rstrip("/"),
        }
        normalize_email_sources([item])
        status = upsert_by_label(cfg["email_sources"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "email", True, status, "邮箱取件来源已录入")

    if args.command == "upsert-account":
        cfg = read_config_file(args.config, allow_missing=True)
        cfg["accounts"] = normalize_accounts(cfg.get("accounts", []))
        password = env_value(env, args.password_env, "账户密码") if args.password_env else ""
        totp_secret = (
            env_value(env, args.totp_secret_env, "TOTP secret") if args.totp_secret_env else ""
        )
        item = {
            "label": args.label.strip(),
            "login_email": args.login_email.strip(),
            "password": password,
            "totp_secret": totp_secret,
            "phone": args.phone.strip(),
            "email": args.email.strip(),
            "note": args.note.strip(),
        }
        normalize_accounts([item])
        status = upsert_by_label(cfg["accounts"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "account", True, status, "账户档案已录入")

    if args.command == "import-freeform":
        if args.stdin == args.from_clipboard:
            raise ConfigCommandError("必须且只能选择 --stdin 或 --from-clipboard")
        if args.stdin:
            raw_text = input_reader() if input_reader else sys.stdin.read()
        else:
            reader = read_clipboard_text if clipboard_reader is None else clipboard_reader
            raw_text = reader()
        parsed = parse_freeform_account_text(raw_text, args.label.strip())
        if parsed["missing"] and args.interactive:
            reader = default_prompt_reader if prompt_reader is None else prompt_reader
            parsed = fill_freeform_missing_fields(parsed, reader)
        if parsed["missing"]:
            return {
                **safe_result(args.label, "import", False, "missing_fields", "自由文本缺少必要字段"),
                "missing": parsed["missing"],
                "preview": parsed["preview"],
            }
        if not args.yes:
            return {
                **safe_result(args.label, "import", False, "needs_confirmation", "请确认脱敏预览后重试 --yes"),
                "preview": parsed["preview"],
            }

        cfg = read_config_file(args.config, allow_missing=True)
        cfg["fixed_sources"] = normalize_fixed_sources(cfg.get("fixed_sources", []))
        cfg["accounts"] = normalize_accounts(cfg.get("accounts", []))
        source_label = args.source_label or f"{args.label.strip()}-SMS"
        upsert_by_label(
            cfg["fixed_sources"],
            {"label": source_label, "phone": parsed["phone"], "url": parsed["sms_url"]},
        )
        upsert_by_label(
            cfg["accounts"],
            {
                "label": args.label.strip(),
                "login_email": parsed["login_email"],
                "password": parsed["password"],
                "totp_secret": parsed["totp_secret"],
                "phone": parsed["phone"],
                "email": "",
                "note": "",
            },
        )
        write_config_file_atomic(args.config, cfg)
        return {
            **safe_result(args.label, "import", True, "imported", "自由文本已脱敏解析并录入"),
            "preview": parsed["preview"],
        }

    if args.command == "validate":
        cfg = read_config_file(args.config, allow_missing=False)
        return validate_config_result(cfg)

    if args.command == "ready-check":
        cfg = read_config_file(args.config, allow_missing=False)
        factory = requests.Session if session_factory is None else session_factory
        if args.all:
            return ready_check_all(cfg, factory)
        return aggregate_results([ready_check_ludan(cfg, factory())])

    if args.command == "disable":
        cfg = read_config_file(args.config, allow_missing=False)
        label = "" if args.kind == "ludan" else args.label.strip()
        if is_disabled(cfg, args.kind, label):
            return safe_result(args.label, args.kind, True, "already_disabled", "已标记为无效")
        disable_source(cfg, args.kind, label, args.reason, args.config)
        return safe_result(args.label, args.kind, True, "disabled", f"已标记无效：{args.reason}")

    if args.command == "enable":
        cfg = read_config_file(args.config, allow_missing=False)
        label = "" if args.kind == "ludan" else args.label.strip()
        removed = enable_source(cfg, args.kind, label, args.config)
        status = "enabled" if removed else "not_disabled"
        reason = "已恢复轮询" if removed else "该项未标记为无效"
        return safe_result(args.label, args.kind, removed, status, reason)

    if args.command == "list-disabled":
        cfg = read_config_file(args.config, allow_missing=False)
        items = list_disabled(cfg)
        return {
            "label": "all",
            "kind": "summary",
            "ready": len(items) == 0,
            "status": "no_disabled" if not items else "has_disabled",
            "sanitized_reason": f"共 {len(items)} 个无效项" if items else "无无效项",
            "items": [{"kind": k, "label": l, "reason": r, "at": t} for k, l, r, t in items],
        }

    if args.command == "prune":
        cfg = read_config_file(args.config, allow_missing=False)
        items = list_disabled(cfg)
        if not items:
            return safe_result("all", "summary", True, "no_disabled", "无无效项可清理")
        preview = [{"kind": k, "label": l, "reason": r, "at": t} for k, l, r, t in items]
        if not args.yes:
            return {
                "label": "all",
                "kind": "summary",
                "ready": False,
                "status": "needs_confirmation",
                "sanitized_reason": f"共 {len(items)} 个无效项，加 --yes 执行清理",
                "items": preview,
            }
        # 执行清理：删除 disabled 的 fixed/email/account 项；LuDan 仅移除标记
        disabled = cfg.get("disabled") or {}
        list_keys = {"fixed": "fixed_sources", "email": "email_sources", "account": "accounts"}
        for key in list(disabled.keys()):
            if key == "ludan":
                continue
            kind, _, label = key.partition(":")
            list_key = list_keys.get(kind)
            if not list_key:
                continue
            cfg[list_key] = [item for item in cfg.get(list_key, []) if item.get("label") != label]
        # 清空 disabled 字典（含 LuDan 标记，LuDan 配置本身保留）
        cfg["disabled"] = {}
        write_config_file_atomic(args.config, cfg)
        return {
            "label": "all",
            "kind": "summary",
            "ready": True,
            "status": "pruned",
            "sanitized_reason": f"已清理 {len(items)} 个无效项",
            "items": preview,
        }

    raise ConfigCommandError("未知 config 子命令")


def _write_windows_clipboard_text(text):
    """用 Win32 Unicode 剪贴板 API 写入，避免 clip 子进程偶发延迟。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_unicode_text = 13
    gmem_moveable = 0x0002

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    data = (str(text) + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
    if not handle:
        return False
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(locked, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(cf_unicode_text, handle):
            return False
        handle = None
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def _write_clip_command_text(text):
    """clip.exe fallback：保留旧路径，给 Win32 API 不可用时兜底。"""
    try:
        proc = subprocess.Popen("clip", stdin=subprocess.PIPE, shell=True)
        proc.communicate(input=str(text).encode("utf-16-le"))
        return proc.returncode == 0
    except Exception:
        return False


def copy_to_clipboard(
    text,
    attempts=3,
    retry_delay=0.05,
    windows_writer=None,
    fallback_writer=None,
):
    """把文本写入剪贴板；Windows API 短暂占用时重试，再退回 clip.exe。"""
    value = str(text)
    total_attempts = max(1, int(attempts))
    writer = windows_writer
    if writer is None and os.name == "nt":
        writer = _write_windows_clipboard_text
    if writer is not None:
        for attempt_index in range(total_attempts):
            try:
                if writer(value):
                    return True
            except Exception:
                pass
            if retry_delay and attempt_index + 1 < total_attempts:
                time.sleep(retry_delay)

    fallback = _write_clip_command_text if fallback_writer is None else fallback_writer
    try:
        return bool(fallback(value))
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

    # 标识来源类型，供 _disable_runtime 按 disabled_key 匹配并从 pollables 移除
    kind = "ludan"

    def __init__(self, cfg, session, request_timeout=3.0):
        self.label = "LuDan"
        self.base_url = cfg["base_url"]
        self.key = cfg["key"]
        self.auto_change = bool(cfg.get("auto_change_on_expire", True))
        self.session = session
        self.request_timeout = request_timeout

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
            msg = data.get("msg", "未知错误")
            code = data.get("code")
            # 不再 sys.exit；抛异常让 SmsMonitor 降级跳过 LuDan，避免拖垮整个面板
            raise LuDanAuthError(f"CDK 校验失败：{msg}（code={code}）", code)
        self.verified_data = data.get("data", {}) or {}
        self.apply_status_data(self.verified_data)
        return data

    def call(self, action):
        """调用开放 API，返回解析后的 dict；网络/限频错误内部重试。"""
        params = {"action": action, "key": self.key}
        for _ in range(3):
            try:
                resp = self.session.get(self.base_url, params=params, timeout=self.request_timeout)
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

    def __init__(self, cfg, session, request_timeout=3.0):
        self.label = cfg["label"]
        self.phone = cfg["phone"]
        self.url = cfg["url"]
        self.session = session
        self.request_timeout = request_timeout

        self.last_code = ""
        self.history = deque(maxlen=8)
        self.status = "等待中"
        self.http_status = "-"
        self.last_checked = "-"
        self.note = ""
        # 硬失败计数：永久性 4xx(401/403/404/410) 或网络异常累计；达阈值由 poll 设置
        # disabled_reason，SmsMonitor 据此持久化跳过。429/5xx/超时/暂无短信不计。
        self.consecutive_hard_failures = 0
        self.disabled_reason = ""
        self.kind = "fixed"
        self.disable_threshold = 5

    def _record_hard_failure(self, reason):
        """累加硬失败计数；达阈值时设置 disabled_reason 供 SmsMonitor 持久化跳过。"""
        self.consecutive_hard_failures += 1
        if self.consecutive_hard_failures >= self.disable_threshold:
            self.disabled_reason = f"连续 {self.consecutive_hard_failures} 次硬失败（{reason}）"

    @property
    def phone_parts(self):
        return split_us_phone(self.phone)

    @property
    def copy_number(self):
        return self.phone_parts.local_number

    def poll(self):
        try:
            resp = self.session.get(self.url, timeout=self.request_timeout)
            self.http_status = str(resp.status_code)
            self.last_checked = now_hms()
            if resp.status_code == 429:
                self.status = "请求过于频繁"
                self.note = "稍后重试"
                # 限频是临时状态，不计硬失败
                self.consecutive_hard_failures = 0
                return
            if not 200 <= resp.status_code < 300:
                self.status = f"HTTP {resp.status_code}"
                self.note = "查询失败"
                if resp.status_code in (401, 403, 404, 410):
                    # 永久性 4xx：链接失效/token 失效，累计硬失败
                    self._record_hard_failure(f"HTTP {resp.status_code}")
                else:
                    # 5xx 或其他 4xx 视为临时，不累计
                    self.consecutive_hard_failures = 0
                return
        except requests.RequestException as e:
            self.last_checked = now_hms()
            self.status = "网络异常"
            self.note = e.__class__.__name__
            self._record_hard_failure(e.__class__.__name__)
            return

        # 成功响应：重置硬失败计数
        self.consecutive_hard_failures = 0
        # yuntl 等平台把纯文本错标成 text/html，只看 Content-Type 会误禁用
        # 裸数字兜底，导致 Google 那种"数字在前"的验证码提取不到；改为看响应
        # 内容是否真有 HTML/XML 标签来决定。
        allow_generic = not looks_like_html(resp.text)
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

    def __init__(self, cfg, session, request_timeout=3.0):
        self.label = cfg["label"]
        self.email = cfg["email"]
        self.provider = cfg["provider"]
        self.base_url = cfg["base_url"]
        self.session = session
        self.request_timeout = request_timeout

        self.last_code = ""
        self.last_mail_id = ""
        self.history = deque(maxlen=8)
        self.status = "等待中"
        self.http_status = "-"
        self.last_checked = "-"
        self.note = ""
        self.consecutive_hard_failures = 0
        self.disabled_reason = ""
        self.kind = "email"
        self.disable_threshold = 5

    def _record_hard_failure(self, reason):
        """累加硬失败计数；达阈值时设置 disabled_reason 供 SmsMonitor 持久化跳过。"""
        self.consecutive_hard_failures += 1
        if self.consecutive_hard_failures >= self.disable_threshold:
            self.disabled_reason = f"连续 {self.consecutive_hard_failures} 次硬失败（{reason}）"

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
            resp = self.session.post(url, json={"email": self.email}, timeout=self.request_timeout)
            self.http_status = str(resp.status_code)
            self.last_checked = now_hms()
            if resp.status_code == 429:
                self.status = "请求过于频繁"
                self.note = "稍后重试"
                self.consecutive_hard_failures = 0
                return
            if not 200 <= resp.status_code < 300:
                self.status = f"HTTP {resp.status_code}"
                self.note = "取件失败"
                if resp.status_code in (401, 403, 404, 410):
                    self._record_hard_failure(f"HTTP {resp.status_code}")
                else:
                    self.consecutive_hard_failures = 0
                return
            payload = resp.json()
        except requests.RequestException as e:
            self.last_checked = now_hms()
            self.status = "网络异常"
            self.note = e.__class__.__name__
            self._record_hard_failure(e.__class__.__name__)
            return
        except ValueError:
            self.last_checked = now_hms()
            self.status = "返回非 JSON"
            self.note = "稍后重试"
            # 非 JSON 视为临时，不累计硬失败
            self.consecutive_hard_failures = 0
            return

        if not payload.get("ok"):
            self.status = "取件失败"
            self.note = str(payload.get("error") or payload.get("detail") or "接口返回 ok=false")
            # ok=false 通常是临时业务态，不累计硬失败
            self.consecutive_hard_failures = 0
            return

        # 取件成功：重置硬失败计数
        self.consecutive_hard_failures = 0
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
        current_totp = self.current_totp
        candidates = [("登录邮箱", self.login_email)]
        if current_totp and current_totp != "密钥无效":
            candidates.append(("2FA 动态码", current_totp))

        # 验证码由轮询链路自动复制；账户菜单只提供登录时需要手动复制的手机号。
        phone_number = ""
        if self.linked_phone_source is not None:
            parts = getattr(self.linked_phone_source, "phone_parts", None)
            phone_number = getattr(parts, "local_number", "") if parts is not None else ""
            if not phone_number:
                phone_number = split_us_phone(getattr(self.linked_phone_source, "phone", "")).local_number
        if not phone_number:
            phone_number = split_us_phone(self.phone).local_number
        candidates.append(("手机号码", phone_number))

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
        self.cfg = cfg
        self.config_path = CONFIG_PATH
        self.poll_interval = max(2, int(cfg.get("poll_interval", 5)))
        self.idle_poll_interval = max(2, int(cfg.get("idle_poll_interval", 15)))
        self.active_after_copy_seconds = max(0, int(cfg.get("active_after_copy_seconds", 180)))
        self.request_timeout = max(0.5, float(cfg.get("request_timeout", 3.0)))
        self.max_poll_workers = max(1, int(cfg.get("max_poll_workers", 4)))
        self.poll_round_timeout = max(
            self.request_timeout, float(cfg.get("poll_round_timeout", self.request_timeout + 0.5))
        )
        self.active_until_code = bool(cfg.get("active_until_code", True))
        self.active_waiting_for_code = False
        self.active_until = 0.0
        # 上一轮超时、尚未完成的 worker：下一轮开始时结算，迟到的验证码补复制，
        # 仍未完成的短期保留；超过保留轮次后允许 fresh retry，避免来源永久下线。
        self._pending_polls: list = []
        self.pending_poll_max_rounds = max(1, int(cfg.get("pending_poll_max_rounds", 3)))
        # 每个 pollable 持有独立 requests.Session，避免多 worker 并发复用同一
        # session 触发连接池/cookie/header 竞态；ready-check 的 session_factory
        # 注入链不走这里，保持不变。
        # 无效来源/账户的展示信息（已在 config.json 持久化标记为 disabled）
        self.disabled_display = list_disabled(cfg)
        # LuDan 已标记失效时不实例化，run() 也不再校验它
        if is_disabled(cfg, "ludan", ""):
            self.ludan = None
        else:
            self.ludan = LuDanSource(cfg, requests.Session(), self.request_timeout)
        self.fixed_sources = [
            FixedUrlSource(item, requests.Session(), self.request_timeout)
            for item in cfg["fixed_sources"]
            if not is_disabled(cfg, "fixed", item.get("label", ""))
        ]
        self.email_sources = [
            EmailSource(item, requests.Session(), self.request_timeout)
            for item in cfg.get("email_sources", [])
            if not is_disabled(cfg, "email", item.get("label", ""))
        ]
        self.accounts = [
            AccountSource(item)
            for item in cfg.get("accounts", [])
            if not is_disabled(cfg, "account", item.get("label", ""))
        ]
        # 需要后台轮询的验证码来源（账户本身不轮询）；ludan 可能 None
        self.pollables = [s for s in [self.ludan, *self.fixed_sources, *self.email_sources] if s is not None]
        # 账户聚合：把电话/邮箱来源挂到账户卡上，避免顶层重复展示
        self._link_accounts()
        self._rebuild_sources()
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

    def _rebuild_sources(self):
        """根据当前 pollables/accounts 重建顶层显示列表（运行时禁用来源后调用）。"""
        linked_ids = set()
        for account in self.accounts:
            if account.linked_phone_source is not None:
                linked_ids.add(id(account.linked_phone_source))
            if account.linked_email_source is not None:
                linked_ids.add(id(account.linked_email_source))
        unlinked = [s for s in self.pollables if id(s) not in linked_ids]
        self.sources = [*unlinked, *self.accounts]

    def _disable_runtime(self, kind, label, reason):
        """运行时把某来源标记无效：写盘 + 更新展示 + 从 pollables 移除。"""
        disable_source(self.cfg, kind, label, reason, self.config_path)
        self.disabled_display = list_disabled(self.cfg)
        target_key = disabled_key(kind, label)
        self.pollables = [
            s for s in self.pollables
            if disabled_key(getattr(s, "kind", ""), getattr(s, "label", "")) != target_key
        ]

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
        if self.disabled_display:
            print("  已禁用（无效来源/账户，config prune 清理 / config enable 恢复）：")
            for kind, label, reason, at in self.disabled_display:
                print(f"    [{kind}] {label} - {reason}（{at}）")
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
                if self.ludan is None:
                    self.note = "LuDan 已禁用，无法换号"
                    self.render()
                    continue
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
            print(f"  {key}. {label}：{value}")
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
        # 只有"高频等码"模式拿到码后才结束高频；定时高频模式（active_until_code=False）
        # 拿到码不应提前清零 active_until，否则会把高频窗口错误切回低频。
        if self.active_until_code:
            self.deactivate_high_frequency()

    def poll_sources(self):
        """并发轮询验证码来源，避免单个慢接口拖住整轮刷新。

        超时的 worker 不会被打断（``future.cancel`` 对已运行任务无效），
        因此本轮先标记超时并把 worker 结转到下一轮结算：迟到的验证码在下一轮
        开始时补复制，仍未完成的回滚关键字段，避免迟到写入让下一轮漏判新码、
        出现"界面显示了验证码但没自动复制"。
        """
        if not self.pollables:
            return []

        new_codes = []
        # 先结算上一轮遗留的超时 worker
        skip_ids, late_codes = self._reconcile_pending_polls()
        new_codes.extend(late_codes)

        targets = [s for s in self.pollables if id(s) not in skip_ids]
        if not targets:
            return new_codes

        worker_count = min(self.max_poll_workers, len(targets))
        executor = ThreadPoolExecutor(max_workers=worker_count)
        snapshots = {id(source): self._snapshot_source_state(source) for source in targets}
        futures = [(source, executor.submit(source.poll)) for source in targets]
        try:
            done, _ = wait([future for _, future in futures], timeout=self.poll_round_timeout)
            disabled_this_round = []
            for source, future in futures:
                if future not in done:
                    future.cancel()
                    self.mark_poll_timeout(source)
                    # 结转到下一轮结算；快照用于异常完成路径的回滚，轮次用于避免永久跳过。
                    self._pending_polls.append((source, future, snapshots[id(source)], 0))
                    continue
                new_code = future.result()
                if new_code:
                    new_codes.append((source.label, new_code))
                # 来源连续硬失败达阈值：持久化标记无效并从 pollables 移除
                if getattr(source, "disabled_reason", ""):
                    disabled_this_round.append(
                        (getattr(source, "kind", ""), source.label, source.disabled_reason)
                    )
            for kind, label, reason in disabled_this_round:
                self._disable_runtime(kind, label, reason)
            if disabled_this_round:
                self._rebuild_sources()
            return new_codes
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _reconcile_pending_polls(self):
        """结算上一轮超时未完成的 worker。

        返回 ``(skip_ids, late_codes)``：

        - 已完成的 worker 若拿到了新验证码，加入 ``late_codes`` 本轮补复制
          （其 ``last_code`` 已由 worker 正确写入，无需回滚）；
        - 已完成但未返回新码却改动了 ``last_code`` 的异常路径，回滚防污染；
        - 仍未完成的 worker 不回滚，避免和仍在运行的线程并发改同一个
          ``history``；短期内加入 ``skip_ids``，达到保留轮次上限后停止追踪并
          允许本轮 fresh retry，避免单个挂死来源永久下线。
        """

        pending = getattr(self, "_pending_polls", None) or []
        self._pending_polls = []
        if not pending:
            return set(), []

        records = [self._unpack_pending_poll(record) for record in pending]
        late_codes = []
        skip_ids = set()
        still_pending = []
        # 限定等待上一轮迟到的 worker 完成
        wait([future for _, future, _, _ in records], timeout=self.poll_round_timeout)
        max_rounds = max(1, int(getattr(self, "pending_poll_max_rounds", 3)))
        for source, future, snap, pending_rounds in records:
            if not future.done():
                next_rounds = pending_rounds + 1
                if next_rounds >= max_rounds:
                    future.cancel()
                    self._refresh_poll_session(source)
                    self.mark_poll_timeout(source)
                    source.note = f"轮询长时间未返回，已重试（>{max_rounds}轮）"
                    continue
                still_pending.append((source, future, snap, next_rounds))
                skip_ids.add(id(source))
                continue
            try:
                code = future.result()
            except Exception:
                code = None
            if code:
                late_codes.append((source.label, code))
                skip_ids.add(id(source))
            elif getattr(source, "last_code", "") != snap.get("last_code"):
                self._rollback_to_snapshot(source, snap)
        self._pending_polls = still_pending
        return skip_ids, late_codes

    @staticmethod
    def _unpack_pending_poll(record):
        """兼容旧三元组 pending 记录，新记录额外保存已保留轮次。"""

        if len(record) == 3:
            source, future, snap = record
            return source, future, snap, 0
        source, future, snap, pending_rounds = record
        return source, future, snap, int(pending_rounds)

    @staticmethod
    def _refresh_poll_session(source):
        """为 fresh retry 换新 session；旧 worker 可能仍在跑，因此不主动关闭旧 session。"""

        if not hasattr(source, "session"):
            return
        source.session = requests.Session()

    @staticmethod
    def _snapshot_source_state(source):
        """快照影响新码判定的关键字段，供超时迟到回滚使用。"""

        history = getattr(source, "history", None)
        return {
            "last_code": getattr(source, "last_code", ""),
            "history": list(history) if history is not None else None,
        }

    @staticmethod
    def _rollback_to_snapshot(source, snap):
        """把 last_code/history 回滚到本轮快照，避免迟到写入污染下一轮判定。"""

        if hasattr(source, "last_code") and source.last_code != snap.get("last_code"):
            source.last_code = snap.get("last_code", "")
        history = getattr(source, "history", None)
        old = snap.get("history")
        if history is not None and old is not None and list(history) != old:
            history.clear()
            history.extend(old)

    def mark_poll_timeout(self, source):
        """只标记本轮轮询超时，不清空已有验证码和历史。"""
        source.note = f"本轮轮询超时（>{self.poll_round_timeout:g}s）"
        if hasattr(source, "status"):
            source.status = "轮询超时"

    def run(self):
        # LuDan 校验失败不再杀进程；标记无效、持久化、跳过，继续运行其余来源
        if self.ludan is not None:
            try:
                print("正在校验 LuDan CDK...")
                self.ludan.verify()
                print("正在获取 LuDan 号码...")
                self.ludan.refresh_number()
            except LuDanAuthError as e:
                print(f"LuDan 已失效，跳过并继续：{e.message}")
                self._disable_runtime("ludan", "", e.message)
                self.ludan = None
                self._rebuild_sources()
        else:
            print("LuDan 已标记禁用，跳过校验。")
        if getattr(self, "disabled_display", None):
            print(f"已跳过 {len(self.disabled_display)} 个无效来源/账户；config list-disabled 查看")
        try:
            while True:
                new_codes = self.poll_sources()
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


def print_config_result(result, as_json=False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(f"{result['kind']}:{result['label']} {result['status']} - {result['sanitized_reason']}")


def run_cli(
    argv=None,
    env=None,
    session_factory=None,
    input_reader=None,
    clipboard_reader=None,
    prompt_reader=None,
):
    """顶层 CLI；无参数和 run 保持原监控行为，config 走标准录入流程。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "run":
        main()
        return 0
    if argv[0] != "config":
        print("未知命令；可用命令：run, config")
        return 2
    try:
        result = run_config_command(
            argv[1:],
            env=env,
            session_factory=session_factory,
            input_reader=input_reader,
            clipboard_reader=clipboard_reader,
            prompt_reader=prompt_reader,
        )
    except ConfigCommandError as e:
        result = safe_result("config", "config", False, "error", str(e))
        json_requested = "--json" in argv
        print_config_result(result, as_json=json_requested)
        return 1
    print_config_result(result, as_json="--json" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
