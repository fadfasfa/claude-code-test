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
import uuid
from copy import deepcopy
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
PRIVATE_IMPORT_PATH = os.path.join(BASE_DIR, "private-import.txt")

EMAIL_PROVIDER_DEFAULTS = {
    "icloud": "https://email.nloop.cc",
    "songniqu": "https://mail.songniqu.cfd",
}

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
    """LuDan 请求失败；只向上层暴露脱敏诊断和是否属于硬失败。"""

    def __init__(self, safe_message, code=None, retryable=False):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.code = code
        self.retryable = retryable


class KkdosApiError(Exception):
    """kkdos 私有接口错误；对外只暴露脱敏诊断。"""

    def __init__(self, safe_message, retryable=False):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retryable = retryable


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
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+1") and len(digits) == 11:
        return PhoneParts(country_code="+1", local_number=digits[1:], raw_digits=digits)
    if len(digits) == 10:
        return PhoneParts(country_code="+1", local_number=digits, raw_digits=digits)
    return PhoneParts(country_code="", local_number=digits, raw_digits=digits)


def normalize_phone(raw):
    """手机号归一化：保留显式 +1 国家码，其余格式只留数字串。

    导入时若只留数字，"+15550123456" 会变成 11 位 "15550123456"，split_us_phone
    因缺少 "+" 无法拆出国家码，复制区域就变成 11 位；保留 +1 后由
    split_us_phone 统一拆成国家码与 10 位本地号。
    """
    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    if text.startswith("+1") and len(digits) == 11:
        return "+" + digits
    return digits


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
    if "已过期" in normalized:
        # eSIM88 等平台号码过期时返回纯文本"已过期"；识别为过期状态，避免
        # 被显示成"未发现验证码"让人以为只是暂无码，实际号码已不可用。
        return FixedSmsParseResult(False, "", "已过期", raw)
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


def html_to_text(text):
    """把 HTML 剥离成纯文本，供固定接码链接的正则提取使用。

    icloud-api.top 等网页接码把验证码埋在 HTML 里，"验证码"关键字和数字之间
    隔着 ``</p>``、换行等标签（实测两者相距数百字符），直接在原始 HTML 上跑
    CODE_PATTERNS 的 30 字符窗口跨不过去，会漏掉验证码；去标签压成单行纯文本后，
    关键字模式即可命中。script/style 整段剔除，避免脚本里的数字干扰。
    """
    raw = text or ""
    without_scripts = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", without_tags).strip()


def mask_email(value):
    if not value or "@" not in value:
        return ""
    name, domain = value.split("@", 1)
    visible = name[:2] if len(name) > 2 else name[:1]
    return f"{visible}***@{domain}"


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


def parse_songniqu_mailbox(value):
    """解析 ``邮箱=Key``，错误信息不包含原始输入。"""
    text = str(value or "").strip()
    email, separator, key = text.partition("=")
    email = email.strip()
    key = key.strip()
    if not separator or not EMAIL_RE.fullmatch(email) or not key:
        raise ConfigCommandError("Songniqu mailbox 格式必须是 邮箱=Key。")
    return email, f"{email}={key}"


def redact_email_secrets(value, mailbox):
    """清除服务端文本中可能回显的完整 mailbox 或查询 Key。"""
    text = str(value or "")
    if not mailbox:
        return text
    _, _, key = str(mailbox).partition("=")
    secrets = sorted({str(mailbox), key}, key=len, reverse=True)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<hidden>")
    return text


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
        normalized = normalize_phone(candidate)
        digits = re.sub(r"\D", "", normalized)
        if 10 <= len(digits) <= 15:
            phone = normalized
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


FREEFORM_FIELD_ALIASES = {
    "label": {"label", "名称", "标签", "账户", "账号名称"},
    "login_email": {"email", "邮箱", "登录邮箱", "账号", "account", "chatgpt谷歌邮箱", "账号邮箱"},
    "password": {"password", "密码", "pass", "pwd", "chatgpt密码", "账号密码"},
    "totp_secret": {"2fa", "totp", "totp_secret", "2fa密钥", "密钥", "一次性安全码密钥"},
    "phone": {"phone", "手机号", "手机", "电话", "号码", "二验手机号", "接码手机号"},
    "sms_url": {"sms_url", "url", "接码url", "接码链接", "短信链接", "二验手机号验证码获取链接", "验证码获取链接"},
}

IGNORED_FREEFORM_ALIASES = {"一次性安全码获取地址", "2fa获取地址"}


def _canonical_freeform_field(name):
    normalized = re.sub(r"[\s_-]+", "", str(name or "").strip().lower())
    for field, aliases in FREEFORM_FIELD_ALIASES.items():
        if normalized in {re.sub(r"[\s_-]+", "", alias.lower()) for alias in aliases}:
            return field
    return ""


def parse_inline_labeled_fields(text):
    """提取同一行连续出现的自然语言字段，不把说明性 2FA 地址当作密钥。"""
    aliases = []
    for field, names in FREEFORM_FIELD_ALIASES.items():
        aliases.extend((name, field) for name in names)
    aliases.extend((name, "") for name in IGNORED_FREEFORM_ALIASES)
    aliases.sort(key=lambda item: len(item[0]), reverse=True)
    marker_re = re.compile(
        r"(?i)(?<![\w])(" + "|".join(re.escape(name) for name, _ in aliases) + r")\s*[:：]"
    )
    alias_map = {re.sub(r"[\s_-]+", "", name.lower()): field for name, field in aliases}
    matches = list(marker_re.finditer(str(text or "")))
    if not matches:
        return {}
    values = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        normalized = re.sub(r"[\s_-]+", "", match.group(1).lower())
        field = alias_map.get(normalized, "")
        value = text[match.end() : end].strip(" \t\r\n,，;；")
        if field and value:
            values[field] = value
    return values


def _parsed_account(values, fallback_label=""):
    login_email = str(values.get("login_email") or "").strip()
    derived_label = login_email.partition("@")[0] if "@" in login_email else ""
    label = str(values.get("label") or fallback_label or derived_label).strip()
    phone = normalize_phone(str(values.get("phone") or ""))
    parsed = {
        "label": label,
        "login_email": login_email,
        "password": str(values.get("password") or ""),
        "totp_secret": str(values.get("totp_secret") or "").strip(),
        "phone": phone,
        "sms_url": str(values.get("sms_url") or "").strip(),
    }
    parsed["preview"] = {
        "label": label,
        "login_email": mask_email(parsed["login_email"]),
        "password": "<hidden>" if parsed["password"] else "",
        "totp_secret": "<hidden>" if parsed["totp_secret"] else "",
        "phone": mask_tail(phone),
        "sms_url": mask_url(parsed["sms_url"]),
    }
    parsed["missing"] = [
        field
        for field in ("label", "login_email", "password", "totp_secret", "phone", "sms_url")
        if not parsed[field]
    ]
    return parsed


def parse_freeform_accounts(text, fallback_label=""):
    """解析一组或多组账户文本；敏感原文只存在于返回的内存结构中。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    blocks = [
        block.strip()
        for block in re.split(r"(?:\r?\n\s*){2,}|^\s*(?:={3,}|-{6,})\s*$", raw, flags=re.MULTILINE)
        if block.strip()
    ]
    parsed_items = []
    for block in blocks:
        values = parse_inline_labeled_fields(block)
        unkeyed = []
        for line in block.splitlines():
            match = re.match(r"^\s*([^:：]{1,24})\s*[:：]\s*(.*?)\s*$", line)
            field = _canonical_freeform_field(match.group(1)) if match else ""
            if field and not values.get(field):
                values[field] = match.group(2)
            elif line.strip():
                unkeyed.append(line.strip())
        if values:
            residue = "\n".join(unkeyed)
            if residue:
                url_match = URL_RE.search(residue)
                if url_match and not values.get("sms_url"):
                    values["sms_url"] = url_match.group(0).rstrip("。).,，；;")
                phone_matches = re.findall(r"\+?\d[\d\s().-]{8,}\d", residue)
                if phone_matches and not values.get("phone"):
                    values["phone"] = phone_matches[-1]
            parsed_items.append(_parsed_account(values, fallback_label if len(blocks) == 1 else ""))
            continue
        parsed_items.append(parse_freeform_account_text(block, fallback_label if len(blocks) == 1 else ""))
    return parsed_items


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
    if key == "YOUR_CDK":
        key = ""
    cfg["key"] = key
    if not key:
        # LuDan 已改为可选来源：key 未配置时跳过 LuDan，仅使用固定/邮箱/账户来源
        print("提示：LuDan key 未配置，将跳过 LuDan 来源。")
    cfg.setdefault("base_url", "https://jm.luudan.xyz/api/open.php")
    cfg.setdefault("poll_interval", 5)
    cfg.setdefault("auto_change_on_expire", True)
    try:
        cfg["fixed_sources"] = normalize_fixed_sources(cfg.get("fixed_sources", []))
        cfg["kkdos_sources"] = normalize_kkdos_sources(cfg.get("kkdos_sources", []))
        cfg["msgnest_sources"] = normalize_msgnest_sources(cfg.get("msgnest_sources", []))
        cfg["email_sources"] = normalize_email_sources(cfg.get("email_sources", []))
        cfg["accounts"] = normalize_accounts(cfg.get("accounts", []))
    except ConfigCommandError as e:
        print(f"配置无效：{e}")
        sys.exit(1)
    return cfg


def normalize_fixed_sources(raw_sources):
    """校验固定来源配置；错误信息不打印真实 URL，避免 token 泄漏到终端。"""
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ConfigCommandError("config.json 里的 fixed_sources 必须是数组。")

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigCommandError(f"fixed_sources 第 {index} 项必须是对象。")
        label = str(item.get("label") or f"固定来源{index}").strip()
        phone = str(item.get("phone") or "").strip()
        url = str(item.get("url") or "").strip()
        if not phone or not url:
            raise ConfigCommandError(f"fixed_sources 第 {index} 项缺少 phone 或 url。")
        sources.append({"label": label, "phone": phone, "url": url})
    return sources


def normalize_kkdos_sources(raw_sources):
    """校验 kkdos 动态来源；错误信息不回显 CDK。"""
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ConfigCommandError("config.json 里的 kkdos_sources 必须是数组。")

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigCommandError(f"kkdos_sources 第 {index} 项必须是对象。")
        label = str(item.get("label") or f"kkdos{index}").strip()
        cdk = str(item.get("cdk") or "").strip()
        base_url = str(item.get("base_url") or "https://sms.kkdos.store").strip().rstrip("/")
        if not cdk:
            raise ConfigCommandError(f"kkdos_sources 第 {index} 项缺少 cdk。")
        sources.append({"label": label, "cdk": cdk, "base_url": base_url})
    return sources


def normalize_msgnest_sources(raw_sources):
    """校验 msg-nest 动态来源；保留持久化字段（claimToken/allocId/fingerprint/phone）。

    重建 dict 时必须带上持久化字段，否则 normalize 会把 redeem 后写回的
    claim_token/alloc_id 等运行时状态剥掉，导致每次启动都重新兑换。
    """
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ConfigCommandError("config.json 里的 msgnest_sources 必须是数组。")

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigCommandError(f"msgnest_sources 第 {index} 项必须是对象。")
        label = str(item.get("label") or f"msgnest{index}").strip()
        cdk = str(item.get("cdk") or "").strip()
        base_url = str(item.get("base_url") or "https://msg-nest.com").strip().rstrip("/")
        if not cdk:
            raise ConfigCommandError(f"msgnest_sources 第 {index} 项缺少 cdk。")
        sources.append(
            {
                "label": label,
                "cdk": cdk,
                "base_url": base_url,
                "fingerprint": str(item.get("fingerprint") or ""),
                "alloc_id": str(item.get("alloc_id") or ""),
                "claim_token": str(item.get("claim_token") or ""),
                "phone": str(item.get("phone") or ""),
            }
        )
    return sources


def normalize_email_sources(raw_sources):
    """校验邮箱接码来源配置；错误信息不打印 email、mailbox 或 Key。"""
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ConfigCommandError("config.json 里的 email_sources 必须是数组。")

    sources = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigCommandError(f"email_sources 第 {index} 项必须是对象。")
        provider = str(item.get("provider") or "icloud").strip().lower()
        if provider not in EMAIL_PROVIDER_DEFAULTS:
            raise ConfigCommandError(
                f"email_sources 第 {index} 项的 provider={provider} 暂不支持。"
            )
        label = str(item.get("label") or f"邮箱来源{index}").strip()
        base_url = str(
            item.get("base_url") or EMAIL_PROVIDER_DEFAULTS[provider]
        ).strip().rstrip("/")
        if provider == "songniqu":
            email, mailbox = parse_songniqu_mailbox(item.get("mailbox"))
            configured_email = str(item.get("email") or "").strip()
            if configured_email and configured_email.casefold() != email.casefold():
                raise ConfigCommandError(
                    f"email_sources 第 {index} 项的 email 与 mailbox 不一致。"
                )
            sources.append(
                {
                    "label": label,
                    "email": email,
                    "provider": provider,
                    "base_url": base_url,
                    "mailbox": mailbox,
                }
            )
            continue

        email = str(item.get("email") or "").strip()
        if not email:
            raise ConfigCommandError(f"email_sources 第 {index} 项缺少 email。")
        sources.append(
            {"label": label, "email": email, "provider": provider, "base_url": base_url}
        )
    return sources


def normalize_accounts(raw_accounts):
    """校验账户档案配置；错误信息不回显密码或 2FA 密钥。"""
    if raw_accounts is None:
        return []
    if not isinstance(raw_accounts, list):
        raise ConfigCommandError("config.json 里的 accounts 必须是数组。")

    accounts = []
    for index, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            raise ConfigCommandError(f"accounts 第 {index} 项必须是对象。")
        login_email = str(item.get("login_email") or "").strip()
        if not login_email:
            raise ConfigCommandError(f"accounts 第 {index} 项缺少 login_email。")
        label = str(item.get("label") or f"账户{index}").strip()
        accounts.append(
            {
                "label": label,
                "login_email": login_email,
                "password": str(item.get("password") or ""),
                "totp_secret": str(item.get("totp_secret") or ""),
                "phone": str(item.get("phone") or "").strip(),
                "phone_source_label": str(item.get("phone_source_label") or "").strip(),
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
        "kkdos_sources": [],
        "msgnest_sources": [],
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
            prefix=os.path.basename(path) + ".",
            suffix=".tmp",
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
        "label": ("账户标签：", False),
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
    parsed["phone"] = normalize_phone(parsed.get("phone") or "")
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


def read_songniqu_mailbox(args, env, prompt_reader):
    """从环境变量或非回显提示读取 Songniqu 凭据。"""
    if args.mailbox_env and args.mailbox_prompt:
        raise ConfigCommandError("--mailbox-env 与 --mailbox-prompt 只能选择一个。")
    if args.mailbox_env:
        raw = env_value(env, args.mailbox_env, "Songniqu mailbox")
    elif args.mailbox_prompt:
        reader = default_prompt_reader if prompt_reader is None else prompt_reader
        raw = reader("Songniqu 邮箱=Key：", secret=True)
    else:
        raise ConfigCommandError("Songniqu 需要 --mailbox-env 或 --mailbox-prompt。")
    return parse_songniqu_mailbox(raw)


def _normalized_phone_source_groups(cfg):
    return {
        "fixed": normalize_fixed_sources(cfg.get("fixed_sources", [])),
        "kkdos": normalize_kkdos_sources(cfg.get("kkdos_sources", [])),
        "msgnest": normalize_msgnest_sources(cfg.get("msgnest_sources", [])),
    }


def select_account_phone_source(account, source_groups, cfg=None):
    """复用运行时优先级，返回目标账号实际会绑定的首个电话来源。"""
    label = str(account.get("phone_source_label") or "").strip()
    ordered = [
        (kind, source)
        for kind in ("fixed", "kkdos", "msgnest")
        for source in source_groups[kind]
        if cfg is None or not is_disabled(cfg, kind, source.get("label", ""))
    ]
    if label:
        for kind, source in ordered:
            if str(source.get("label") or "") == label:
                return kind, source
        return None, None

    phone = account.get("phone")
    if phone:
        for source in source_groups["fixed"]:
            if cfg is not None and is_disabled(cfg, "fixed", source.get("label", "")):
                continue
            if same_phone(phone, source.get("phone")):
                return "fixed", source
    return None, None


def focus_account_preview(cfg, login_prefix):
    """生成单账号收敛候选的非敏感骨架；不读取新邮箱凭据。"""
    prefix = str(login_prefix or "").strip().casefold()
    if not prefix:
        raise ConfigCommandError("--login-prefix 不能为空。")
    accounts = normalize_accounts(cfg.get("accounts", []))
    matches = []
    for account in accounts:
        login_email = str(account.get("login_email") or "")
        local_name, separator, _ = login_email.partition("@")
        if separator and local_name.casefold().startswith(prefix):
            matches.append(account)
    if len(matches) != 1:
        raise ConfigCommandError(
            f"login_email 前缀匹配必须恰好 1 条，当前命中 {len(matches)} 条。"
        )

    target = deepcopy(matches[0])
    source_groups = _normalized_phone_source_groups(cfg)
    phone_kind, phone_source = select_account_phone_source(target, source_groups, cfg)
    if phone_source is not None:
        target["phone_source_label"] = str(phone_source.get("label") or "")
    else:
        target["phone_source_label"] = ""

    counts = {
        "accounts": len(accounts),
        "fixed": len(source_groups["fixed"]),
        "kkdos": len(source_groups["kkdos"]),
        "msgnest": len(source_groups["msgnest"]),
        "email": len(normalize_email_sources(cfg.get("email_sources", []))),
        "disabled": len(list_disabled(cfg)),
    }
    after = {
        "accounts": 1,
        "fixed": 1 if phone_kind == "fixed" else 0,
        "kkdos": 1 if phone_kind == "kkdos" else 0,
        "msgnest": 1 if phone_kind == "msgnest" else 0,
        "email": 1,
        "disabled": 0,
    }
    result = {
        **safe_result(
            mask_email(target["login_email"]),
            "focus",
            False,
            "needs_confirmation",
            "唯一目标已匹配；提供 Songniqu 凭据并加 --yes 后执行原子收敛",
        ),
        "target_email": mask_email(target["login_email"]),
        "phone_source_kind": phone_kind or "none",
        "counts": {
            kind: {
                "before": counts[kind],
                "after": after[kind],
                "removed": max(0, counts[kind] - after[kind]),
            }
            for kind in counts
        },
    }
    return result, target, source_groups, phone_kind, phone_source


def build_focused_config(cfg, target, phone_kind, phone_source, email_item):
    candidate = deepcopy(cfg)
    for kind, key in (
        ("fixed", "fixed_sources"),
        ("kkdos", "kkdos_sources"),
        ("msgnest", "msgnest_sources"),
    ):
        candidate[key] = [deepcopy(phone_source)] if phone_kind == kind else []
    target = deepcopy(target)
    target["email"] = email_item["email"]
    candidate["accounts"] = [target]
    candidate["email_sources"] = [deepcopy(email_item)]
    candidate["key"] = ""
    candidate["disabled"] = {}
    validate_config_result(candidate)
    return candidate


def private_template_text(count=1):
    """生成不含示例秘密的私密导入空白槽位。"""
    count = max(1, int(count))
    block = "名称：\n邮箱：\n密码：\n2FA：\n手机号：\n接码链接："
    return ("\n\n".join(block for _ in range(count)) + "\n")


def write_text_atomic(path, text):
    """原子写入本地文本；错误消息不包含文件内容。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(os.path.abspath(path)),
            prefix=os.path.basename(path) + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            f.write(text)
        os.replace(tmp_path, path)
    except OSError as e:
        raise ConfigCommandError(f"私密模板写入失败：{e.__class__.__name__}") from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def private_template_has_values(path):
    """判断私密模板是否已填写；调用方不得输出读取到的原文。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            parsed_items = parse_freeform_accounts(f.read())
    except OSError as e:
        raise ConfigCommandError(f"私密模板不可读：{e.__class__.__name__}") from e
    return any(
        any(item.get(field) for field in ("login_email", "password", "totp_secret", "phone", "sms_url"))
        for item in parsed_items
    )


def analyze_import_batch(parsed_items, cfg, source_label="", result_label="batch"):
    """构建脱敏预览和待写入副本；不修改传入配置。"""
    fixed_sources = normalize_fixed_sources(cfg.get("fixed_sources", []))
    accounts = normalize_accounts(cfg.get("accounts", []))
    existing_by_label = {item["label"]: item for item in accounts}
    existing_email_labels = {
        item["login_email"].strip().lower(): item["label"] for item in accounts
    }
    batch_labels = set()
    batch_emails = {}
    conflicts = []
    if source_label and len(parsed_items) > 1:
        conflicts.append({"label": source_label, "reason": "批量导入不能共用同一个 --source-label"})
    for item in parsed_items:
        label = item["label"]
        email = item["login_email"].strip().lower()
        if label and label in batch_labels:
            conflicts.append({"label": label, "reason": "批次内 label 重复"})
        if email and email in batch_emails and batch_emails[email] != label:
            conflicts.append({"label": label, "reason": "批次内登录邮箱重复"})
        current_label = existing_email_labels.get(email)
        if current_label and current_label != label:
            conflicts.append({"label": label, "reason": "登录邮箱已属于其他 label"})
        batch_labels.add(label)
        if email:
            batch_emails[email] = label

    missing_items = [
        {"label": item["label"] or f"第 {index} 组", "missing": item["missing"]}
        for index, item in enumerate(parsed_items, start=1)
        if item["missing"]
    ]
    changes = [
        {
            "label": item["label"],
            "action": "updated" if item["label"] in existing_by_label else "created",
            "source_label": source_label or f"{item['label']}-SMS",
        }
        for item in parsed_items
        if item["label"]
    ]
    response = {
        "items": [item["preview"] for item in parsed_items],
        "summary": {
            "total": len(parsed_items),
            "created": sum(change["action"] == "created" for change in changes),
            "updated": sum(change["action"] == "updated" for change in changes),
        },
        "conflicts": conflicts,
        "changes": changes,
    }
    if len(parsed_items) == 1:
        response["preview"] = parsed_items[0]["preview"]
    if missing_items or conflicts:
        return {
            **safe_result(result_label, "import", False, "invalid_batch", "批量导入需要补齐或解决冲突"),
            **response,
            "missing": missing_items,
        }, None

    for item in parsed_items:
        item_source_label = source_label or f"{item['label']}-SMS"
        upsert_by_label(
            fixed_sources,
            {"label": item_source_label, "phone": item["phone"], "url": item["sms_url"]},
        )
        upsert_by_label(
            accounts,
            {
                "label": item["label"],
                "login_email": item["login_email"],
                "password": item["password"],
                "totp_secret": item["totp_secret"],
                "phone": item["phone"],
                "phone_source_label": item_source_label,
                "email": "",
                "note": "",
            },
        )
    updated_cfg = dict(cfg)
    updated_cfg["fixed_sources"] = normalize_fixed_sources(fixed_sources)
    updated_cfg["accounts"] = normalize_accounts(accounts)
    return response, updated_cfg


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


def ludan_configured(cfg):
    """LuDan 是否已配置：key 非空且非占位符 YOUR_CDK。"""
    key = (cfg.get("key") or "").strip()
    return bool(key) and key != "YOUR_CDK"


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

    # 与 FixedUrlSource.poll 保持一致：真 HTML 先去标签再解析，避免网页接码
    # 因关键字与数字间隔标签而判"未发现验证码"。
    text = resp.text
    allow_generic = not looks_like_html(text)
    if not allow_generic:
        text = html_to_text(text)
    result = parse_fixed_sms_response(text, allow_generic=allow_generic)
    return safe_result(label, "fixed", True, "ready", result.status)


def ready_check_kkdos_source(cfg, session, request_timeout=3.0):
    """kkdos verify 能拿到手机号即 ready；不启动等码窗口。"""
    label = cfg.get("label") or "kkdos"
    source = KkdosSource(cfg, session, request_timeout)
    try:
        source.verify()
    except KkdosApiError as e:
        return safe_result(label, "kkdos", False, "api_not_ready", e.safe_message)
    if source.phone:
        return safe_result(label, "kkdos", True, "ready", "已分配可用号码")
    return safe_result(label, "kkdos", False, "number_unavailable", "未返回可用号码")


def ready_check_msgnest_source(cfg, session, request_timeout=3.0):
    """msg-nest redeem+verify 能拿到手机号即 ready；不持久化、不启动等码。"""
    label = cfg.get("label") or "msgnest"
    source = MsgNestSource(cfg, session, request_timeout)
    try:
        source.verify()
    except MsgNestApiError as e:
        return safe_result(label, "msgnest", False, "api_not_ready", e.safe_message)
    if source.phone:
        return safe_result(label, "msgnest", True, "ready", "已分配可用号码")
    return safe_result(label, "msgnest", False, "number_unavailable", "未返回可用号码")


def email_request_spec(cfg):
    """返回邮箱 provider 的请求地址和 JSON；调用方不得打印请求体。"""
    provider = cfg["provider"]
    base_url = cfg["base_url"].rstrip("/")
    if provider == "songniqu":
        return (
            f"{base_url}/api/receive",
            {"mailbox": cfg["mailbox"], "turnstile_token": ""},
        )
    return f"{base_url}/api/{provider}/query", {"email": cfg["email"]}


def email_api_failure(payload):
    """把邮箱接口失败映射为固定脱敏状态；成功时返回 ``None``。"""
    if payload.get("ok"):
        return None
    code = str(payload.get("code") or "")
    if code.startswith("turnstile_"):
        return "turnstile_required", "需要网页人机验证"
    if code == "mailbox_bound":
        return "mailbox_bound", "需要在网页处理邮箱绑定"
    return "api_not_ready", "取件接口拒绝请求"


def email_http_failure(resp):
    """把邮箱 HTTP 失败转换为固定脱敏结果；绝不返回响应正文。"""
    if resp.status_code == 429:
        return "rate_limited", "请求过于频繁"
    try:
        payload = resp.json()
    except (AttributeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        failure = email_api_failure(payload)
        if failure:
            return failure
    return "http_error", f"HTTP {resp.status_code}"


def extract_email_payload_code(payload):
    """按顶层 code、邮件 code、正文解析的顺序提取最新验证码。"""
    mails = payload.get("mails") or []
    mail = mails[0] if mails and isinstance(mails[0], dict) else {}
    direct_candidates = [payload.get("code"), mail.get("verification_code")]
    for candidate in direct_candidates:
        code = str(candidate or "").strip()
        if re.fullmatch(r"\d{4,8}", code):
            return code, mail

    subject = str(mail.get("subject") or "")
    body = str(mail.get("body") or mail.get("preview") or "")
    result = parse_fixed_sms_response(f"{subject} {body}", allow_generic=True)
    return (result.code if result.has_sms else ""), mail


def ready_check_email_source(cfg, session, request_timeout=3.0):
    """邮箱取件接口 ok=true 即 ready；不要求已有验证码邮件。"""
    label = cfg.get("label") or "email"
    url, body = email_request_spec(cfg)
    try:
        resp = session.post(url, json=body, timeout=request_timeout)
    except requests.RequestException as e:
        return safe_result(label, "email", False, "network_error", e.__class__.__name__)

    if not 200 <= resp.status_code < 300:
        status, reason = email_http_failure(resp)
        return safe_result(label, "email", False, status, reason)
    try:
        payload = resp.json()
    except (AttributeError, ValueError):
        return safe_result(label, "email", False, "bad_response", "接口返回非 JSON")
    if not isinstance(payload, dict):
        return safe_result(label, "email", False, "bad_response", "接口返回结构无效")
    failure = email_api_failure(payload)
    if failure:
        status, reason = failure
        return safe_result(label, "email", False, status, reason)
    return safe_result(label, "email", True, "ready", "取件接口可用")


def same_phone(left, right):
    return bool(left and right and split_us_phone(left).raw_digits == split_us_phone(right).raw_digits)


def ready_check_account(account, phone_sources, email_sources):
    """账户本地字段可用且已配置的接码关联能匹配时为 ready。"""
    label = account.get("label") or "account"
    if account.get("totp_secret"):
        try:
            generate_totp(account["totp_secret"])
        except ValueError:
            return safe_result(label, "account", False, "bad_totp", "TOTP 密钥无效")

    phone_source_label = (account.get("phone_source_label") or "").strip()
    if phone_source_label:
        if not any((src.get("label") or "").strip() == phone_source_label for src in phone_sources):
            return safe_result(label, "account", False, "phone_unlinked", "关联电话来源 label 未匹配")
    elif account.get("phone") and not any(same_phone(account["phone"], src.get("phone")) for src in phone_sources):
        return safe_result(label, "account", False, "phone_unlinked", "关联电话未匹配接码来源")
    if account.get("email"):
        target = account["email"].strip().lower()
        if not any((src.get("email") or "").strip().lower() == target for src in email_sources):
            return safe_result(label, "account", False, "email_unlinked", "关联邮箱未匹配取件来源")
    return safe_result(label, "account", True, "ready", "账户字段和关联可用")


def validate_config_result(cfg):
    """只验证结构和必要字段，不触发真实网络。"""
    normalize_fixed_sources(cfg.get("fixed_sources", []))
    normalize_kkdos_sources(cfg.get("kkdos_sources", []))
    normalize_msgnest_sources(cfg.get("msgnest_sources", []))
    normalize_email_sources(cfg.get("email_sources", []))
    normalize_accounts(cfg.get("accounts", []))
    # LuDan 已改为可选来源：key 未配置不再视为配置无效
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
    kkdos_sources = normalize_kkdos_sources(cfg.get("kkdos_sources", []))
    msgnest_sources = normalize_msgnest_sources(cfg.get("msgnest_sources", []))
    email_sources = normalize_email_sources(cfg.get("email_sources", []))
    accounts = normalize_accounts(cfg.get("accounts", []))

    results = []
    # LuDan 可选：key 未配置时跳过其就绪检查，不计入整体 ready 判定
    if ludan_configured(cfg):
        results.append(ready_check_ludan(cfg, session_factory()))
    for source in fixed_sources:
        results.append(ready_check_fixed_source(source, session_factory(), request_timeout))
    for source in kkdos_sources:
        results.append(ready_check_kkdos_source(source, session_factory(), request_timeout))
    for source in msgnest_sources:
        results.append(ready_check_msgnest_source(source, session_factory(), request_timeout))
    for source in email_sources:
        results.append(ready_check_email_source(source, session_factory(), request_timeout))
    for account in accounts:
        results.append(ready_check_account(account, [*fixed_sources, *kkdos_sources, *msgnest_sources], email_sources))
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
    email_parser.add_argument("--email")
    email_parser.add_argument("--provider", default="icloud")
    email_parser.add_argument("--base-url")
    email_parser.add_argument("--mailbox-env")
    email_parser.add_argument("--mailbox-prompt", action="store_true")

    kkdos_parser = subparsers.add_parser("upsert-kkdos")
    add_common(kkdos_parser)
    kkdos_parser.add_argument("--label", required=True)
    kkdos_parser.add_argument("--cdk-env", required=True)
    kkdos_parser.add_argument("--base-url", default="https://sms.kkdos.store")

    msgnest_parser = subparsers.add_parser("upsert-msgnest")
    add_common(msgnest_parser)
    msgnest_parser.add_argument("--label", required=True)
    msgnest_parser.add_argument("--cdk-env", required=True)
    msgnest_parser.add_argument("--base-url", default="https://msg-nest.com")
    msgnest_parser.add_argument("--alloc-id", default="")
    msgnest_parser.add_argument("--phone", default="")

    account_parser = subparsers.add_parser("upsert-account")
    add_common(account_parser)
    account_parser.add_argument("--label", required=True)
    account_parser.add_argument("--login-email", required=True)
    account_parser.add_argument("--password-env")
    account_parser.add_argument("--totp-secret-env")
    account_parser.add_argument("--phone", default="")
    account_parser.add_argument("--phone-source-label", default="")
    account_parser.add_argument("--email", default="")
    account_parser.add_argument("--note", default="")

    import_parser = subparsers.add_parser("import-freeform")
    add_common(import_parser)
    import_parser.add_argument("--label", default="")
    import_parser.add_argument("--source-label")
    import_parser.add_argument("--stdin", action="store_true")
    import_parser.add_argument("--from-clipboard", action="store_true")
    import_parser.add_argument("--interactive", action="store_true")
    import_parser.add_argument("--yes", action="store_true")

    private_template_parser = subparsers.add_parser("private-template")
    add_common(private_template_parser)
    private_template_parser.add_argument("--count", type=int, default=1)
    private_template_parser.add_argument("--open", action="store_true")
    private_template_parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    private_template_parser.add_argument("--file", default=PRIVATE_IMPORT_PATH, help=argparse.SUPPRESS)

    private_import_parser = subparsers.add_parser("import-private")
    add_common(private_import_parser)
    private_import_parser.add_argument("--yes", action="store_true")
    private_import_parser.add_argument("--file", default=PRIVATE_IMPORT_PATH, help=argparse.SUPPRESS)

    validate_parser = subparsers.add_parser("validate")
    add_common(validate_parser)

    ready_parser = subparsers.add_parser("ready-check")
    add_common(ready_parser)
    ready_parser.add_argument("--all", action="store_true")

    disable_parser = subparsers.add_parser("disable")
    add_common(disable_parser)
    disable_parser.add_argument("--label", required=True)
    disable_parser.add_argument(
        "--kind", required=True, choices=["ludan", "fixed", "kkdos", "msgnest", "email", "account"]
    )
    disable_parser.add_argument("--reason", default="手动禁用")

    enable_parser = subparsers.add_parser("enable")
    add_common(enable_parser)
    enable_parser.add_argument("--label", required=True)
    enable_parser.add_argument(
        "--kind", required=True, choices=["ludan", "fixed", "kkdos", "msgnest", "email", "account"]
    )

    list_disabled_parser = subparsers.add_parser("list-disabled")
    add_common(list_disabled_parser)

    prune_parser = subparsers.add_parser("prune")
    add_common(prune_parser)
    prune_parser.add_argument("--yes", action="store_true")

    focus_parser = subparsers.add_parser("focus-account")
    add_common(focus_parser)
    focus_parser.add_argument("--login-prefix", required=True)
    focus_parser.add_argument("--mailbox-env")
    focus_parser.add_argument("--mailbox-prompt", action="store_true")
    focus_parser.add_argument("--email-label", default="Songniqu")
    focus_parser.add_argument("--base-url", default=EMAIL_PROVIDER_DEFAULTS["songniqu"])
    focus_parser.add_argument("--yes", action="store_true")
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
        provider = args.provider.strip().lower()
        base_url = (args.base_url or EMAIL_PROVIDER_DEFAULTS.get(provider) or "").strip().rstrip("/")
        if provider == "songniqu":
            email, mailbox = read_songniqu_mailbox(args, env, prompt_reader)
            if args.email and args.email.strip().casefold() != email.casefold():
                raise ConfigCommandError("--email 与 Songniqu mailbox 中的邮箱不一致。")
            item = {
                "label": args.label.strip(),
                "email": email,
                "provider": provider,
                "base_url": base_url,
                "mailbox": mailbox,
            }
        else:
            if args.mailbox_env or args.mailbox_prompt:
                raise ConfigCommandError("只有 songniqu provider 支持 mailbox 输入。")
            item = {
                "label": args.label.strip(),
                "email": str(args.email or "").strip(),
                "provider": provider,
                "base_url": base_url,
            }
        item = normalize_email_sources([item])[0]
        status = upsert_by_label(cfg["email_sources"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "email", True, status, "邮箱取件来源已录入")

    if args.command == "upsert-kkdos":
        cfg = read_config_file(args.config, allow_missing=True)
        cfg["kkdos_sources"] = normalize_kkdos_sources(cfg.get("kkdos_sources", []))
        cdk = env_value(env, args.cdk_env, "kkdos CDK")
        item = {
            "label": args.label.strip(),
            "cdk": cdk,
            "base_url": args.base_url.strip().rstrip("/"),
        }
        normalize_kkdos_sources([item])
        status = upsert_by_label(cfg["kkdos_sources"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "kkdos", True, status, "kkdos 动态来源已录入")

    if args.command == "upsert-msgnest":
        cfg = read_config_file(args.config, allow_missing=True)
        cfg["msgnest_sources"] = normalize_msgnest_sources(cfg.get("msgnest_sources", []))
        cdk = env_value(env, args.cdk_env, "msg-nest CDK")
        # 保留已持久化的运行时状态（fingerprint/alloc_id/claim_token/phone），
        # 除非命令显式传入新值；避免重录 CDK 时丢掉已兑换的 claimToken。
        existing = {}
        for item in cfg["msgnest_sources"]:
            if item.get("label") == args.label.strip():
                existing = item
                break
        item = {
            "label": args.label.strip(),
            "cdk": cdk,
            "base_url": args.base_url.strip().rstrip("/"),
            "fingerprint": existing.get("fingerprint", ""),
            "alloc_id": args.alloc_id.strip() or existing.get("alloc_id", ""),
            "claim_token": existing.get("claim_token", ""),
            "phone": args.phone.strip() or existing.get("phone", ""),
        }
        normalize_msgnest_sources([item])
        status = upsert_by_label(cfg["msgnest_sources"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "msgnest", True, status, "msg-nest 动态来源已录入")

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
            "phone_source_label": args.phone_source_label.strip(),
            "email": args.email.strip(),
            "note": args.note.strip(),
        }
        normalize_accounts([item])
        status = upsert_by_label(cfg["accounts"], item)
        write_config_file_atomic(args.config, cfg)
        return safe_result(args.label, "account", True, status, "账户档案已录入")

    if args.command == "private-template":
        if args.count < 1:
            raise ConfigCommandError("私密模板账户数量必须大于 0")
        if not args.force and private_template_has_values(args.file):
            raise ConfigCommandError("私密模板已有填写内容，拒绝覆盖；请先导入或显式使用 --force")
        write_text_atomic(args.file, private_template_text(args.count))
        if args.open:
            if os.name != "nt" or not hasattr(os, "startfile"):
                raise ConfigCommandError("当前平台不支持自动打开模板")
            try:
                os.startfile(args.file)
            except OSError as e:
                raise ConfigCommandError(f"私密模板已创建，但打开失败：{e.__class__.__name__}") from e
        return {
            **safe_result("private-template", "config", True, "initialized", "私密空白模板已准备"),
            "count": args.count,
            "opened": bool(args.open),
        }

    if args.command == "import-freeform":
        if args.stdin == args.from_clipboard:
            raise ConfigCommandError("必须且只能选择 --stdin 或 --from-clipboard")
        if args.stdin:
            raw_text = input_reader() if input_reader else sys.stdin.read()
        else:
            reader = read_clipboard_text if clipboard_reader is None else clipboard_reader
            raw_text = reader()
        parsed_items = parse_freeform_accounts(raw_text, args.label.strip())
        if not parsed_items:
            raise ConfigCommandError("未识别到可导入的账户资料")
        if args.interactive:
            reader = default_prompt_reader if prompt_reader is None else prompt_reader
            parsed_items = [
                fill_freeform_missing_fields(item, reader) if item["missing"] else item
                for item in parsed_items
            ]

        cfg = read_config_file(args.config, allow_missing=True)
        response, updated_cfg = analyze_import_batch(
            parsed_items, cfg, source_label=args.source_label or "", result_label=args.label or "batch"
        )
        if updated_cfg is None:
            return response
        if not args.yes:
            return {
                **safe_result(args.label or "batch", "import", False, "needs_confirmation", "请确认脱敏预览后重试 --yes"),
                **response,
            }
        write_config_file_atomic(args.config, updated_cfg)
        return {
            **safe_result(args.label or "batch", "import", True, "imported", "批量资料已脱敏解析并原子录入"),
            **response,
        }

    if args.command == "import-private":
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                raw_text = f.read()
        except OSError as e:
            raise ConfigCommandError(f"私密模板不可读：{e.__class__.__name__}") from e
        parsed_items = parse_freeform_accounts(raw_text)
        if not parsed_items:
            raise ConfigCommandError("私密模板为空或未识别到账户槽位")
        cfg = read_config_file(args.config, allow_missing=True)
        response, updated_cfg = analyze_import_batch(
            parsed_items, cfg, result_label="private"
        )
        if updated_cfg is None:
            return response
        if not args.yes:
            return {
                **safe_result("private", "import", False, "needs_confirmation", "请确认脱敏预览后重试 --yes"),
                **response,
            }
        write_config_file_atomic(args.config, updated_cfg)
        try:
            write_text_atomic(args.file, private_template_text(len(parsed_items)))
        except ConfigCommandError:
            return {
                **safe_result("private", "import", False, "imported_cleanup_failed", "配置已导入，但私密模板清空失败"),
                **response,
            }
        return {
            **safe_result("private", "import", True, "imported", "私密资料已导入，模板已恢复为空白槽位"),
            **response,
        }

    if args.command == "validate":
        cfg = read_config_file(args.config, allow_missing=False)
        return validate_config_result(cfg)

    if args.command == "ready-check":
        cfg = read_config_file(args.config, allow_missing=False)
        factory = requests.Session if session_factory is None else session_factory
        if args.all:
            return ready_check_all(cfg, factory)
        # LuDan 可选：key 未配置时跳过就绪检查，不发无效请求
        if not ludan_configured(cfg):
            return aggregate_results([safe_result("LuDan", "ludan", True, "not_configured", "LuDan key 未配置，已跳过")])
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

    if args.command == "focus-account":
        cfg = read_config_file(args.config, allow_missing=False)
        preview, target, source_groups, phone_kind, phone_source = focus_account_preview(
            cfg, args.login_prefix
        )
        if not args.yes:
            return preview
        email, mailbox = read_songniqu_mailbox(args, env, prompt_reader)
        email_item = normalize_email_sources(
            [
                {
                    "label": args.email_label.strip() or "Songniqu",
                    "provider": "songniqu",
                    "base_url": args.base_url,
                    "email": email,
                    "mailbox": mailbox,
                }
            ]
        )[0]
        candidate = build_focused_config(
            cfg, target, phone_kind, phone_source, email_item
        )
        factory = requests.Session if session_factory is None else session_factory
        check = ready_check_email_source(
            email_item,
            factory(),
            max(0.5, float(candidate.get("request_timeout", 3.0))),
        )
        if not check["ready"]:
            return {
                **preview,
                "status": check["status"],
                "sanitized_reason": check["sanitized_reason"],
            }
        write_config_file_atomic(args.config, candidate)
        return {
            **preview,
            "ready": True,
            "status": "focused",
            "sanitized_reason": "Songniqu 已验证，配置已原子收敛为唯一目标账号",
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
        list_keys = {
            "fixed": "fixed_sources",
            "kkdos": "kkdos_sources",
            "msgnest": "msgnest_sources",
            "email": "email_sources",
            "account": "accounts",
        }
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
    supports_change_number = True

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
            code = data.get("code")
            retryable = bool(data.get("_local_failure"))
            reason = "LuDan 暂时无法连接" if retryable else f"LuDan 认证失败（code={code}）"
            raise LuDanAuthError(reason, code, retryable=retryable)
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
                if resp.status_code in (401, 403):
                    return {"code": resp.status_code, "msg": "认证失败"}
                return resp.json()
            except requests.RequestException as e:
                self.note = f"网络异常重试中（{e.__class__.__name__}）"
                time.sleep(2)
            except ValueError:
                self.note = "接口返回非 JSON，稍后重试"
                time.sleep(2)
        return {"code": -1, "msg": "本地请求失败", "_local_failure": True}

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
            self.note = "换号失败：服务端拒绝请求"

    def poll(self):
        """轮询验证码；处理新验证码与号码过期。"""
        data = self.call("get_code")
        if data.get("code") != 0:
            self.note = "查询失败"
            return
        d = data.get("data", {})
        self.apply_status_data(d)

        if d.get("has_sms"):
            code = d.get("sms_code") or d.get("code", "")
            content = d.get("content", "")
            if not code and content:
                parsed = parse_fixed_sms_response(str(content), allow_generic=True)
                code = parsed.code if parsed.has_sms else ""
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


class KkdosSource:
    """kkdos 动态号码来源：verify 取号，复制后 start，并通过 SSE 等验证码。"""

    kind = "kkdos"
    supports_change_number = True
    poll_only_when_active = True

    def __init__(self, cfg, session, request_timeout=3.0):
        self.label = cfg["label"]
        self.cdk = cfg["cdk"]
        self.base_url = cfg.get("base_url", "https://sms.kkdos.store").rstrip("/")
        self.session = session
        self.request_timeout = request_timeout

        self.session_id = ""
        self.phone = ""
        self.state = "未校验"
        self.card_type = "-"
        self.locked = False
        self.attempt = None
        self.max_attempts = None
        self.trial_switch_count = 0
        self.max_trial_switches = None
        self.expires_at = ""
        self.effective_expires_at = ""
        self.waiting_for_code = False
        self.last_code = ""
        self.history = deque(maxlen=8)
        self.status = "等待校验"
        self.last_checked = "-"
        self.note = ""
        self.disabled_reason = ""
        self.verify_hard_failures = 0
        self.verify_failure_threshold = 3

    @property
    def phone_parts(self):
        return split_us_phone(self.phone)

    @property
    def copy_number(self):
        return self.phone_parts.local_number

    def _post_json(self, path, body=None):
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.post(url, json=body or {}, timeout=self.request_timeout)
        except requests.RequestException as e:
            raise KkdosApiError(f"临时网络错误（{e.__class__.__name__}）", retryable=True) from e
        self.last_checked = now_hms()
        status_code = getattr(resp, "status_code", 0)
        if not 200 <= status_code < 300:
            retryable = status_code not in (401, 403)
            message = f"临时 HTTP 错误（{status_code}）" if retryable else "kkdos 认证失败"
            raise KkdosApiError(message, retryable=retryable)
        try:
            payload = resp.json()
        except (AttributeError, ValueError) as e:
            raise KkdosApiError("接口返回非 JSON", retryable=True) from e
        if not payload.get("success"):
            data = payload.get("data") or {}
            if isinstance(data, dict) and "remainingSeconds" in data:
                raise KkdosApiError(f"请再等待 {data.get('remainingSeconds')} 秒", retryable=True)
            raise KkdosApiError("kkdos 请求被拒绝")
        return payload.get("data") or {}

    def verify(self):
        data = self._post_json("/api/cdk/verify", {"cdk": self.cdk.strip().upper()})
        self.apply_status_data(data)
        self.status = "已分配号码" if self.phone else "未返回号码"
        return data

    def apply_status_data(self, data):
        if not isinstance(data, dict):
            return
        self.session_id = str(data.get("sessionId") or self.session_id)
        self.phone = str(data.get("phone") or data.get("phoneNumber") or self.phone)
        self.state = str(data.get("state") or self.state)
        self.card_type = str(data.get("type") or self.card_type)
        self.locked = bool(data.get("locked", self.locked))
        self.attempt = data.get("attempt", self.attempt)
        self.max_attempts = data.get("maxAttempts", self.max_attempts)
        self.trial_switch_count = data.get("trialSwitchCount", self.trial_switch_count)
        self.max_trial_switches = data.get("maxTrialSwitches", self.max_trial_switches)
        self.expires_at = str(data.get("expiresAt") or self.expires_at)
        self.effective_expires_at = str(data.get("effectiveExpiresAt") or self.effective_expires_at)
        for item in data.get("history") or []:
            self._record_history_item(item)

    def _record_history_item(self, item):
        if isinstance(item, dict):
            # kkdos history 把验证码放在 code 字段（实测确认）；
            # 旧结构才用 content/data/message 文本，保留 fallback。
            code = str(item.get("code") or "").strip()
            text = str(item.get("content") or item.get("data") or item.get("message") or "")
        else:
            code = ""
            text = str(item or "")
        if not code:
            result = parse_fixed_sms_response(text, allow_generic=True)
            if not result.has_sms or not result.code:
                return
            code = result.code
            text = result.content or text
        if any(c == code for _, c, _ in self.history):
            return
        self.last_code = code
        self.history.appendleft((now_hms(), code, text))

    def start_waiting_for_code(self):
        self.waiting_for_code = True
        self.note = "等待验证码查询"

    def _record_verify_error(self, error):
        self.waiting_for_code = False
        self.note = error.safe_message
        if error.retryable:
            self.verify_hard_failures = 0
            return
        self.verify_hard_failures += 1
        if self.verify_hard_failures >= self.verify_failure_threshold:
            self.disabled_reason = "kkdos 连续认证失败"

    def poll(self):
        if not self.session_id:
            try:
                self.verify()
            except KkdosApiError as e:
                # verify 失败（网络异常/HTTP错误/CDK失效）不应拖垮整个监控，
                # 与下方 start 调用一致：设状态后跳过本轮，等下次重试。
                self.status = "校验失败"
                self._record_verify_error(e)
                return None
            self.verify_hard_failures = 0
        if not self.waiting_for_code:
            self.status = "等待触发"
            return None
        try:
            data = self._post_json(f"/api/session/{self.session_id}/start")
        except KkdosApiError as e:
            self.status = "启动失败"
            self._record_verify_error(e)
            return None
        self.apply_status_data(data)
        return self._read_sse_code()

    def _read_sse_code(self):
        url = f"{self.base_url}/api/sse/{self.session_id}"
        try:
            resp = self.session.get(url, stream=True, timeout=self.request_timeout)
        except requests.RequestException as e:
            self.status = "SSE 网络异常"
            self.note = e.__class__.__name__
            return None
        self.last_checked = now_hms()
        if not 200 <= getattr(resp, "status_code", 0) < 300:
            self.status = f"SSE HTTP {getattr(resp, 'status_code', '-')}"
            self.note = "查询失败"
            return None
        try:
            try:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                    if not text.startswith("data:"):
                        continue
                    try:
                        event = json.loads(text.partition(":")[2].strip())
                    except ValueError:
                        continue
                    code = self._handle_sse_event(event)
                    if code:
                        return code
                    if event.get("type") in {"idle", "failed", "timeout", "error"}:
                        return None
            except requests.RequestException as e:
                self.status = "SSE 网络异常"
                self.note = e.__class__.__name__
                return None
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        return None

    def _handle_sse_event(self, event):
        if not isinstance(event, dict):
            return None
        if "remainingSeconds" in event:
            self.note = f"剩余等待 {event.get('remainingSeconds')} 秒"
        self.apply_status_data(event)
        event_type = event.get("type")
        if event_type in {"connected", "retry"}:
            self.status = "正在等待验证码"
            return None
        if event_type == "code":
            content = str(event.get("data") or event.get("content") or "")
            # kkdos SSE 与 history 同源，验证码可能在 code 字段（实测 history 用
            # code）；保留 data/content 文本解析 fallback，兼容旧结构。
            code_direct = str(event.get("code") or "").strip()
            if code_direct:
                code = code_direct
                display_content = content or code_direct
            else:
                result = parse_fixed_sms_response(content, allow_generic=True)
                code = result.code if result.has_sms else ""
                display_content = result.content or content
            if code and code != self.last_code:
                self.last_code = code
                self.history.appendleft((now_hms(), code, display_content))
                self.status = "收到新验证码"
                self.note = "收到新验证码"
                self.waiting_for_code = False
                return code
            self.status = "收到短信"
            self.note = "未提取到新验证码"
            return None
        if event_type == "idle":
            self.status = "等待触发"
            self.note = "本次未收到验证码，可重新查询或换号"
            self.waiting_for_code = False
            return None
        if event_type in {"failed", "timeout", "error"}:
            self.status = "查询失败"
            self.note = "验证码查询失败"
            self.waiting_for_code = False
        return None

    def change_number(self):
        if self.locked:
            self.note = "号码已锁定，不能换号"
            return
        if not self.session_id:
            try:
                self.verify()
            except KkdosApiError as e:
                self.note = e.safe_message
                return
        try:
            data = self._post_json(f"/api/session/{self.session_id}/switch-phone")
        except KkdosApiError as e:
            self.note = f"换号失败：{e.safe_message}"
            return
        self.apply_status_data(data)
        self.last_code = ""
        self.waiting_for_code = False
        self.status = "已换号"
        self.note = "已换号"

    def status_text(self):
        attempt = "-"
        if self.attempt is not None or self.max_attempts is not None:
            attempt = f"{self.attempt or '-'} / {self.max_attempts or '-'}"
        locked = "是" if self.locked else "否"
        return f"状态:{self.status} | 尝试:{attempt} | 锁定:{locked} | 更新时间:{self.last_checked}"


class MsgNestApiError(Exception):
    """msg-nest 私有接口错误；对外只暴露脱敏诊断。"""

    def __init__(self, safe_message, retryable=False):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retryable = retryable


class MsgNestSource:
    """msg-nest 动态号码来源：redeem CDK 换 claimToken，轮询 messages 取验证码。

    与 kkdos 的差异：claimToken 走 ``x-claim-token`` 请求头（非 sessionId），
    取码用 GET 轮询（非 SSE），claimToken 会过期需 re-redeem。
    claimToken/allocId/fingerprint 持久化到 config.json，到期或缺失时自动重新兑换。
    """

    kind = "msgnest"
    supports_change_number = True

    def __init__(self, cfg, session, request_timeout=3.0, monitor_cfg=None, config_path=None):
        self.label = cfg["label"]
        self.cdk = cfg["cdk"]
        self.base_url = str(cfg.get("base_url") or "https://msg-nest.com").rstrip("/")
        self.session = session
        self.request_timeout = request_timeout
        # 持久化状态：redeem 后写回 config.json；ready-check 不传则只读
        self.fingerprint = str(cfg.get("fingerprint") or "")
        self.alloc_id = str(cfg.get("alloc_id") or "")
        self.claim_token = str(cfg.get("claim_token") or "")
        self.phone = self._normalize_phone(cfg.get("phone") or "")
        # 号码已锁定（录入时种子）；redeem 后核对尾号，不符告警不阻断
        self.expected_phone_tail = self._phone_tail(cfg.get("phone") or "")
        self._monitor_cfg = monitor_cfg
        self._config_path = config_path

        self.expires_at = ""
        self.last_code = ""
        self.history = deque(maxlen=8)
        self.status = "等待校验"
        self.last_checked = "-"
        self.note = ""
        self.disabled_reason = ""
        self.verify_hard_failures = 0
        self.verify_failure_threshold = 3

    @staticmethod
    def _normalize_phone(raw):
        """统一成 10 位北美号码或原始数字串；避免 +1 前缀污染复制区域。"""
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits

    @staticmethod
    def _phone_tail(raw):
        return re.sub(r"\D", "", str(raw or ""))[-10:]

    @property
    def phone_parts(self):
        return split_us_phone(self.phone)

    @property
    def copy_number(self):
        return self.phone_parts.local_number

    def _headers(self, with_claim=True):
        # msg-nest 在 Cloudflare 后，补 browser 头避免被挑战页拦截
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.base_url}/",
        }
        if with_claim and self.claim_token:
            headers["x-claim-token"] = self.claim_token
        return headers

    def _request(self, path, method="GET", body=None, with_claim=True):
        url = f"{self.base_url}/api/public{path}"
        headers = self._headers(with_claim)
        try:
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=self.request_timeout)
            else:
                headers["content-type"] = "application/json"
                resp = self.session.post(url, headers=headers, json=body, timeout=self.request_timeout)
        except requests.RequestException as e:
            raise MsgNestApiError(f"临时网络错误（{e.__class__.__name__}）", retryable=True) from e
        self.last_checked = now_hms()
        status_code = getattr(resp, "status_code", 0)
        if not 200 <= status_code < 300:
            if status_code == 401:
                # claimToken 过期：交给上层 ensure_token/verify 重新 redeem
                raise MsgNestApiError("claimToken 已过期", retryable=True)
            retryable = status_code not in (403, 404, 410)
            message = f"临时 HTTP 错误（{status_code}）" if retryable else "msg-nest 认证失败或资源不可用"
            raise MsgNestApiError(message, retryable=retryable)
        try:
            return resp.json()
        except (AttributeError, ValueError) as e:
            raise MsgNestApiError("接口返回非 JSON", retryable=True) from e

    def _persist_state(self):
        """把 fingerprint/alloc_id/claim_token/phone 写回 config.json；仅 redeem 成功后调用。"""
        if self._monitor_cfg is None or self._config_path is None:
            return
        for item in self._monitor_cfg.get("msgnest_sources", []):
            if item.get("label") == self.label:
                item["fingerprint"] = self.fingerprint
                item["alloc_id"] = self.alloc_id
                item["claim_token"] = self.claim_token
                item["phone"] = self.phone
                break
        write_config_file_atomic(self._config_path, self._monitor_cfg)

    def redeem(self):
        """POST /cdks/redeem 换取 claimToken 与号码；幂等核对 expected_phone 尾号。"""
        if not self.fingerprint:
            self.fingerprint = str(uuid.uuid4())
        payload = self._request(
            "/cdks/redeem",
            method="POST",
            body={"code": self.cdk, "fingerprint": self.fingerprint},
            with_claim=False,
        )
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        data = data or {}
        self.alloc_id = str(data.get("allocId") or data.get("allocationId") or self.alloc_id)
        self.claim_token = str(data.get("claimToken") or self.claim_token)
        phone = data.get("phone") or data.get("phoneNumber") or ""
        if phone:
            self.phone = self._normalize_phone(phone)
        self.expires_at = str(data.get("expiresAt") or self.expires_at)
        # 号码核对：与录入种子尾号不符则告警，不阻断（服务端可能已换号）
        if self.expected_phone_tail:
            tail = self._phone_tail(self.phone)
            if tail and tail != self.expected_phone_tail:
                self.note = f"号码与预期不符（预期 ***{self.expected_phone_tail[-4:]}）"
            else:
                self.note = "已兑换"
        else:
            self.note = "已兑换"
        self.status = "已分配号码" if self.phone else "未返回号码"
        self.verify_hard_failures = 0
        self._persist_state()
        return data

    def ensure_token(self):
        """claimToken 或 allocId 缺失时重新 redeem。"""
        if not self.claim_token or not self.alloc_id:
            self.redeem()

    def verify(self):
        """GET /allocations/{id} 刷新号码/状态；401 时 re-redeem 重试一次。"""
        self.ensure_token()
        try:
            payload = self._request(f"/allocations/{self.alloc_id}")
        except MsgNestApiError as e:
            if not e.retryable:
                self.status = "校验失败"
                self._record_verify_error(e)
                raise
            # 401/临时错误：清 token 重新兑换再查一次
            self.claim_token = ""
            self.redeem()
            payload = self._request(f"/allocations/{self.alloc_id}")
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        data = data or {}
        phone = data.get("phone") or data.get("phoneNumber") or ""
        if phone:
            self.phone = self._normalize_phone(phone)
        self.expires_at = str(data.get("expiresAt") or self.expires_at)
        self.status = "已分配号码" if self.phone else "未返回号码"
        self.verify_hard_failures = 0
        return data

    def poll(self):
        """GET /allocations/{id}/messages 轮询验证码；401 自动 re-redeem。"""
        try:
            self.ensure_token()
        except MsgNestApiError as e:
            self.status = "兑换失败"
            self._record_verify_error(e)
            return None
        try:
            payload = self._request(f"/allocations/{self.alloc_id}/messages")
        except MsgNestApiError as e:
            if not e.retryable:
                self.status = "查询失败"
                self._record_verify_error(e)
                return None
            # 401/临时错误：重新兑换后再取一次
            self.status = "token 过期，重新兑换"
            try:
                self.claim_token = ""
                self.redeem()
                payload = self._request(f"/allocations/{self.alloc_id}/messages")
            except MsgNestApiError as e2:
                self.status = "取码失败"
                self._record_verify_error(e2)
                return None
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            messages = data.get("messages") or data.get("sms") or []
        elif isinstance(data, list):
            messages = data
        else:
            messages = []
        self.verify_hard_failures = 0
        if not messages:
            self.status = "暂无短信"
            return None
        # messages 按时间倒序返回（最新在前）。遇到 last_code 即停止遍历：
        # 否则当最新码已见过时，循环会落到更旧的历史码上，把它当成"新码"
        # 返回并复制，表现为"收到的不是最新验证码、反而是更旧的"。
        for msg in messages:
            if isinstance(msg, dict):
                # msg-nest 把验证码直接放在 code 字段（实测确认）；
                # 旧结构/其他平台才用 content 文本，保留 fallback。
                code_direct = str(msg.get("code") or "").strip()
                content = str(msg.get("content") or msg.get("body") or msg.get("text") or msg.get("message") or "")
            else:
                code_direct = ""
                content = str(msg or "")
            if code_direct:
                code = code_direct
                display_content = content or code_direct
            else:
                result = parse_fixed_sms_response(content, allow_generic=True)
                code = result.code if result.has_sms else ""
                display_content = result.content or content
            if not code:
                continue
            if code == self.last_code:
                break  # 已见过的码，之后的更旧，不再当新码
            self.last_code = code
            self.history.appendleft((now_hms(), code, display_content))
            self.status = "收到新验证码"
            self.note = "收到新验证码"
            return code
        self.status = "收到短信"
        self.note = "未提取到新验证码"
        return None

    def _record_verify_error(self, error):
        self.note = error.safe_message
        if error.retryable:
            self.verify_hard_failures = 0
            return
        self.verify_hard_failures += 1
        if self.verify_hard_failures >= self.verify_failure_threshold:
            self.disabled_reason = "msg-nest 连续认证失败"

    def change_number(self):
        if not self.alloc_id:
            try:
                self.redeem()
            except MsgNestApiError as e:
                self.note = e.safe_message
                return
        try:
            self._request(
                f"/allocations/{self.alloc_id}/replace-number",
                method="POST",
                body={"reason": "manual"},
            )
        except MsgNestApiError as e:
            self.note = f"换号失败：{e.safe_message}"
            return
        self.last_code = ""
        self.status = "已换号"
        self.note = "已换号（需重新兑换取号）"

    def expire_text(self):
        return str(self.expires_at)[:19] if self.expires_at else "-"

    def status_text(self):
        return f"状态:{self.status} | 有效期:{self.expire_text()} | 更新时间:{self.last_checked}"


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
        # 真正的 HTML（icloud-api.top 等网页接码）先去标签再解析：原始 HTML 里
        # "验证码"关键字和数字之间隔着 </p>、换行等标签，正则 30 字符窗口跨不
        # 过去会漏掉验证码；压成单行纯文本后关键字模式即可命中。
        text = resp.text
        allow_generic = not looks_like_html(text)
        if not allow_generic:
            text = html_to_text(text)
        result = parse_fixed_sms_response(text, allow_generic=allow_generic)
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
    """邮箱接码来源；兼容 iCloud query 与 Songniqu mailbox 取件。"""

    # 渲染时据此把"可复制号码"文案切换为"邮箱地址"
    is_email = True

    def __init__(self, cfg, session, request_timeout=3.0):
        self.label = cfg["label"]
        self.email = cfg["email"]
        self.provider = cfg["provider"]
        self.base_url = cfg["base_url"]
        self.mailbox = cfg.get("mailbox", "")
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
        url, body = email_request_spec(
            {
                "provider": self.provider,
                "base_url": self.base_url,
                "email": self.email,
                "mailbox": self.mailbox,
            }
        )
        try:
            resp = self.session.post(url, json=body, timeout=self.request_timeout)
            self.http_status = str(resp.status_code)
            self.last_checked = now_hms()
            if resp.status_code == 429:
                self.status = "请求过于频繁"
                self.note = "稍后重试"
                self.consecutive_hard_failures = 0
                return
            if not 200 <= resp.status_code < 300:
                failure_status, failure_reason = email_http_failure(resp)
                self.status = {
                    "turnstile_required": "需要网页验证",
                    "mailbox_bound": "需要网页绑定",
                    "api_not_ready": "取件失败",
                }.get(failure_status, f"HTTP {resp.status_code}")
                self.note = failure_reason
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

        if not isinstance(payload, dict):
            self.status = "返回结构无效"
            self.note = "稍后重试"
            self.consecutive_hard_failures = 0
            return

        failure = email_api_failure(payload)
        if failure:
            status, reason = failure
            self.status = {
                "turnstile_required": "需要网页验证",
                "mailbox_bound": "需要网页绑定",
            }.get(status, "取件失败")
            self.note = reason
            # ok=false 通常是临时业务态，不累计硬失败
            self.consecutive_hard_failures = 0
            return

        # 取件成功：重置硬失败计数
        self.consecutive_hard_failures = 0
        code, mail = extract_email_payload_code(payload)
        mails = payload.get("mails") or []
        if not mails and not code:
            self.status = "暂无邮件"
            return

        self.status = "已取件"
        mail_id = str(mail.get("id") or mail.get("message_id") or "")
        subject = redact_email_secrets(mail.get("subject") or "", self.mailbox)
        body = redact_email_secrets(
            mail.get("body") or mail.get("preview") or "", self.mailbox
        )
        if code and code != self.last_code:
            self.last_code = code
            self.last_mail_id = mail_id
            content = subject or body or "邮箱验证码"
            self.history.appendleft((now_hms(), code, content))
            self.note = "收到新验证码"
            return code
        if not code:
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
        self.phone_source_label = cfg.get("phone_source_label", "")
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
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        # 上一轮超时、尚未完成的 worker：下一轮开始时结算，迟到的验证码补复制，
        # 仍未完成的短期保留；超过保留轮次后允许 fresh retry，避免来源永久下线。
        self._pending_polls: list = []
        self.pending_poll_max_rounds = max(1, int(cfg.get("pending_poll_max_rounds", 3)))
        # 每个 pollable 持有独立 requests.Session，避免多 worker 并发复用同一
        # session 触发连接池/cookie/header 竞态；ready-check 的 session_factory
        # 注入链不走这里，保持不变。
        # 无效来源/账户的展示信息（已在 config.json 持久化标记为 disabled）
        self.disabled_display = list_disabled(cfg)
        # LuDan 可选：key 未配置或已标记失效时不实例化，run() 也不再校验它
        if not ludan_configured(cfg) or is_disabled(cfg, "ludan", ""):
            self.ludan = None
        else:
            self.ludan = LuDanSource(cfg, requests.Session(), self.request_timeout)
        self.fixed_sources = [
            FixedUrlSource(item, requests.Session(), self.request_timeout)
            for item in cfg["fixed_sources"]
            if not is_disabled(cfg, "fixed", item.get("label", ""))
        ]
        self.kkdos_sources = [
            KkdosSource(item, requests.Session(), self.request_timeout)
            for item in cfg.get("kkdos_sources", [])
            if not is_disabled(cfg, "kkdos", item.get("label", ""))
        ]
        self.msgnest_sources = [
            MsgNestSource(
                item,
                requests.Session(),
                self.request_timeout,
                monitor_cfg=cfg,
                config_path=self.config_path,
            )
            for item in cfg.get("msgnest_sources", [])
            if not is_disabled(cfg, "msgnest", item.get("label", ""))
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
        self.pollables = [
            s for s in [self.ludan, *self.fixed_sources, *self.kkdos_sources, *self.msgnest_sources, *self.email_sources]
            if s is not None
        ]
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
        enabled_phone_sources = [
            source for source in self.pollables if getattr(source, "kind", "") in {"fixed", "kkdos", "msgnest"}
        ]
        enabled_fixed_sources = [
            source for source in enabled_phone_sources if getattr(source, "kind", "") == "fixed"
        ]
        enabled_email_sources = [
            source for source in self.pollables if getattr(source, "kind", "") == "email"
        ]
        for account in self.accounts:
            account.linked_phone_source = None
            account.linked_email_source = None
            if account.phone_source_label:
                for src in enabled_phone_sources:
                    if src.label == account.phone_source_label:
                        account.linked_phone_source = src
                        break
            elif account.phone:
                target = split_us_phone(account.phone).raw_digits
                for src in enabled_fixed_sources:
                    if target and split_us_phone(src.phone).raw_digits == target:
                        account.linked_phone_source = src
                        break
            if account.email:
                target = account.email.strip().lower()
                for src in enabled_email_sources:
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
        self.unlinked_sources = unlinked
        self.sources = [*self.accounts, *unlinked]

    def _disable_runtime(self, kind, label, reason):
        """运行时把某来源标记无效：写盘 + 更新展示 + 从 pollables 移除。"""
        disable_source(self.cfg, kind, label, reason, self.config_path)
        self.disabled_display = list_disabled(self.cfg)
        target_key = disabled_key(kind, label)
        self.pollables = [
            s for s in self.pollables
            if disabled_key(getattr(s, "kind", ""), getattr(s, "label", "")) != target_key
        ]
        self._link_accounts()
        self._rebuild_sources()

    def render(self):
        clear_screen()
        line = "=" * 68
        print(line)
        print("                    SMS 验证码多来源监控")
        print(line)
        print()
        account_count = len(self.accounts)
        if account_count:
            print("  账户摘要")
        for index, source in enumerate(self.sources, start=1):
            if index == account_count + 1 and self.unlinked_sources:
                print("  其他来源")
            self.render_source(index, source)
            print()
        if self.disabled_display:
            print("  已禁用（无效来源/账户，config prune 清理 / config enable 恢复）：")
            for kind, label, reason, at in self.disabled_display:
                print(f"    [{kind}] {label} - {reason}（{at}）")
            print()
        if msvcrt:
            keys = f"1-{len(self.sources)}" if self.sources else "无"
            change_hint = " | n 换号" if self.changeable_sources() else ""
            print(f"  热键：按 {keys} 复制对应号码/邮箱{change_hint} | q 退出")
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
        history = list(source.history)
        if history:
            for ts, code, content in history:
                print(f"        [{ts}] {code}   {snippet(content)}")
        else:
            print("        （暂无）")

    def render_account_source(self, index, source):
        print(f"  [{index}] {source.label}（账户）")
        print(f"      登录邮箱：{source.login_email}")
        print(f"      密码：{'已配置' if source.password else '未配置'}")
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
            disabled_reason = self._disabled_reason("fixed", source.phone_source_label)
            state = f"来源已禁用：{disabled_reason}" if disabled_reason else "未关联"
            print(f"      短信号码：{parts.country_code or '-'} {parts.local_number}（{state}）")

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

    def _disabled_reason(self, kind, label):
        for disabled_kind, disabled_label, reason, _ in self.disabled_display:
            if disabled_kind == kind and disabled_label == label:
                return reason or "已禁用"
        return ""

    def handle_keys(self):
        """非阻塞读取热键；返回 False 表示请求退出。"""
        if not msvcrt:
            return True
        while msvcrt.kbhit():
            ch = msvcrt.getwch().lower()
            if ch == "q":
                self._digit_buffer = ""
                return False
            if ch == "n":
                source = self.default_changeable_source()
                if source is None:
                    self.note = "当前没有可换号来源"
                    self.render()
                    continue
                self.note = f"{source.label} 手动换号中..."
                self.render()
                source.change_number()
                self.activate_high_frequency()
                self.render()  # 立即刷新换号结果，不等下一轮轮询
                continue
            if ch.isdigit():
                self._digit_buffer += ch
                self._digit_deadline = time.time() + 0.35
        if self._digit_buffer and time.time() >= self._digit_deadline:
            index_text = self._digit_buffer
            self._digit_buffer = ""
            if not index_text.startswith("0"):
                self.copy_source_number(int(index_text))
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
            self.mark_waiting_for_code(source)
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
        print("  按对应数字复制；q 或 Esc 取消。")

    def copy_account_field(self, index, account):
        fields = account.copy_fields()
        if not fields:
            self.note = f"[{index}] {account.label} 暂无可复制项"
            return
        if not msvcrt:
            _, label, value = fields[0]
            ok = copy_to_clipboard(value)
            if ok:
                if label == "手机号码":
                    self.mark_waiting_for_code(account.linked_phone_source)
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
            if ch in {"q", "\x1b"}:
                self.note = f"已取消 [{index}] {account.label} 复制"
                self.render()
                return
            if ch in choices:
                label, value = choices[ch]
                ok = copy_to_clipboard(value)
                if ok:
                    if label == "手机号码":
                        self.mark_waiting_for_code(account.linked_phone_source)
                    self.activate_high_frequency()
                self.note = f"已复制 [{index}] {account.label} 的{label}" if ok else "复制失败"
                self.render()
                return

    @staticmethod
    def mark_waiting_for_code(source):
        if source is None:
            return
        starter = getattr(source, "start_waiting_for_code", None)
        if starter:
            starter()

    def changeable_sources(self):
        return [
            source for source in self.pollables
            if getattr(source, "supports_change_number", False)
        ]

    def default_changeable_source(self):
        sources = self.changeable_sources()
        if not sources:
            return None
        return sources[0]

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

        targets = [
            s for s in self.pollables
            if id(s) not in skip_ids
            and (
                not getattr(s, "poll_only_when_active", False)
                or getattr(s, "waiting_for_code", False)
            )
        ]
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
                try:
                    new_code = future.result()
                except Exception as e:
                    # 来源 poll() 漏网异常不应拖垮整轮；按无新码处理，等下次重试。
                    new_code = None
                    source.note = f"轮询异常：{e.__class__.__name__}"
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
                print(f"LuDan 校验未通过：{e.safe_message}")
                if not e.retryable:
                    self._disable_runtime("ludan", "", e.safe_message)
                    self.ludan = None
                    self._rebuild_sources()
        else:
            print("LuDan 未启用（未配置 key 或已禁用），跳过校验。")
        for source in list(getattr(self, "kkdos_sources", [])):
            try:
                print(f"正在校验 {source.label} kkdos CDK...")
                source.verify()
            except KkdosApiError as e:
                print(f"{source.label} kkdos 暂不可用，跳过并继续：{e.safe_message}")
                if not e.retryable:
                    self._disable_runtime("kkdos", source.label, e.safe_message)
        for source in list(getattr(self, "msgnest_sources", [])):
            try:
                print(f"正在校验 {source.label} msg-nest CDK...")
                source.verify()
            except MsgNestApiError as e:
                print(f"{source.label} msg-nest 暂不可用，跳过并继续：{e.safe_message}")
                if not e.retryable:
                    self._disable_runtime("msgnest", source.label, e.safe_message)
        self._rebuild_sources()
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
    summary = result.get("summary") or {}
    if summary:
        print(
            f"账户总数：{summary.get('total', 0)} | 新增：{summary.get('created', 0)} | "
            f"更新：{summary.get('updated', 0)}"
        )
    counts = result.get("counts") or {}
    if counts:
        print("配置收敛计数：")
        for kind, values in counts.items():
            print(
                f"  {kind}: {values.get('before', 0)} -> {values.get('after', 0)} "
                f"(移除 {values.get('removed', 0)})"
            )
    for index, item in enumerate(result.get("items") or [], start=1):
        print(
            f"  [{index}] {item.get('label') or '未命名'} | {item.get('login_email') or '邮箱缺失'} | "
            f"密码:{'已配置' if item.get('password') else '缺失'} | "
            f"2FA:{'已配置' if item.get('totp_secret') else '缺失'} | "
            f"手机:{item.get('phone') or '缺失'} | URL:{item.get('sms_url') or '缺失'}"
        )
    for conflict in result.get("conflicts") or []:
        print(f"  冲突：{conflict.get('label') or '未命名'} - {conflict.get('reason')}")
    for missing in result.get("missing") or []:
        print(f"  缺失：{missing.get('label')} - {', '.join(missing.get('missing') or [])}")


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
    if result.get("status") == "imported_cleanup_failed":
        return 1
    if result.get("kind") == "focus" and result.get("status") not in {
        "needs_confirmation",
        "focused",
    }:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
