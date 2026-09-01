#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sms-monitor 解析规则测试。

本文件只覆盖固定文本接码链接的本地解析逻辑，避免真实 HTTP 响应里的到期时间
被误判成验证码。运行方式：`python sms-monitor/test_monitor_parser.py`。
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import monitor
from monitor import (
    AccountSource,
    ConfigCommandError,
    EmailSource,
    FixedUrlSource,
    KkdosSource,
    LuDanSource,
    MsgNestSource,
    SmsMonitor,
    generate_totp,
    normalize_accounts,
    normalize_email_sources,
    normalize_kkdos_sources,
    normalize_msgnest_sources,
    normalize_phone,
    parse_fixed_sms_response,
    parse_freeform_account_text,
    parse_freeform_accounts,
    private_template_text,
    ready_check_all,
    ready_check_email_source,
    ready_check_fixed_source,
    ready_check_kkdos_source,
    ready_check_ludan,
    ready_check_msgnest_source,
    run_cli,
    run_config_command,
    split_us_phone,
    totp_remaining,
    validate_config_result,
)


class FakeJsonResponse:
    """测试 LuDan API 解析用的最小响应对象，避免访问真实服务。"""

    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeTextResponse:
    """测试固定文本来源用的最小 HTTP 响应对象。"""

    def __init__(self, text, status_code=200, content_type="text/plain"):
        self.text = text
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class FakeSession:
    """按调用顺序返回预设 payload，并记录 action。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.actions = []

    def get(self, url, params, timeout):
        self.actions.append(params["action"])
        return FakeJsonResponse(self.payloads.pop(0))


class TimeoutRecordingSession:
    """记录请求 timeout，验证所有来源都使用配置值。"""

    def __init__(self):
        self.get_timeouts = []
        self.post_timeouts = []

    def get(self, url, params=None, timeout=None):
        self.get_timeouts.append(timeout)
        if params:
            return FakeJsonResponse({"code": 0, "data": {"has_sms": False}})
        return FakeTextResponse("Your verification code is 123456")

    def post(self, url, json=None, timeout=None):
        self.post_timeouts.append(timeout)
        return FakeJsonResponse(
            {"ok": True, "mails": [{"id": "m1", "subject": "Code 654321", "body": ""}]}
        )


class ActionSession:
    """按 LuDan action 返回 payload，用于 ready-check 测试。"""

    def __init__(self, payloads):
        self.payloads = payloads
        self.actions = []

    def get(self, url, params=None, timeout=None):
        action = params["action"]
        self.actions.append(action)
        return FakeJsonResponse(self.payloads[action].pop(0))


class FixedReadySession:
    """固定 URL ready-check 用的最小 session。"""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc

    def get(self, url, timeout=None):
        if self.exc:
            raise self.exc
        return self.response


class EmailReadySession:
    """邮箱 ready-check 用的最小 session。"""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        if self.exc:
            raise self.exc
        return self.response


class FakeSseResponse:
    """kkdos SSE 测试响应；只实现 KkdosSource 需要的 iter_lines。"""

    def __init__(self, events, status_code=200):
        self.events = list(events)
        self.status_code = status_code
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        for event in self.events:
            raw = f"data: {json.dumps(event, ensure_ascii=False)}"
            yield raw if decode_unicode else raw.encode("utf-8")

    def close(self):
        pass


class KkdosFakeSession:
    """记录 kkdos HTTP 调用，并按路径返回预设 payload/SSE。"""

    def __init__(self, verify=None, start=None, switch=None, sse=None):
        self.verify = verify or {}
        self.start = start or {}
        self.switch = switch or {}
        self.sse = sse or []
        self.posts = []
        self.gets = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        if url.endswith("/api/cdk/verify"):
            return FakeJsonResponse(self.verify)
        if url.endswith("/start"):
            return FakeJsonResponse(self.start)
        if url.endswith("/switch-phone"):
            return FakeJsonResponse(self.switch)
        return FakeJsonResponse({"success": False, "error": "unexpected post"})

    def get(self, url, stream=False, timeout=None):
        self.gets.append((url, stream, timeout))
        return FakeSseResponse(self.sse)


class FakePollable:
    """测试并发轮询用的最小验证码来源。"""

    def __init__(self, label, code=None, delay=0):
        self.label = label
        self.code = code
        self.delay = delay
        self.note = ""
        self.status = "等待中"
        self.last_code = "OLD"
        self.history = [("12:00:00", "OLD", "old message")]
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.code


class StatefulPollable:
    """模拟真实来源：poll() 内部写 last_code/history 并返回新码。

    FakePollable 不写状态，无法复现"迟到 worker 静默改写 last_code 导致下一轮
    漏判新码"的竞态；本类按 FixedUrlSource 的契约在 poll 内更新状态。
    """

    def __init__(self, label, code, delay=0):
        self.label = label
        self.code = code
        self.delay = delay
        self.last_code = ""
        self.history = []
        self.note = ""
        self.status = "等待中"
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.code and self.code != self.last_code:
            self.last_code = self.code
            self.history.insert(0, ("00:00:00", self.code, ""))
            self.note = "收到新验证码"
            return self.code
        return None


class GatedStatefulPollable:
    """用事件精确控制 poll 完成时机，避免慢源测试依赖真实 sleep。"""

    def __init__(self, label, code=None, *, write_before_release=False):
        self.label = label
        self.code = code
        self.write_before_release = write_before_release
        self.last_code = ""
        self.history = []
        self.note = ""
        self.status = "等待中"
        self.calls = 0
        self._lock = threading.Lock()
        self._release_events = []
        self._finished_events = []

    def poll(self):
        with self._lock:
            self.calls += 1
            release_event = threading.Event()
            finished_event = threading.Event()
            self._release_events.append(release_event)
            self._finished_events.append(finished_event)
        try:
            if self.write_before_release:
                self._write_code()
            release_event.wait(timeout=5)
            if self.code and not self.write_before_release:
                self._write_code()
                return self.code
            return self.code if self.write_before_release else None
        finally:
            finished_event.set()

    def _write_code(self):
        if self.code and self.code != self.last_code:
            self.last_code = self.code
            self.history.insert(0, ("00:00:00", self.code, ""))
            self.note = "收到新验证码"

    def release_all(self):
        with self._lock:
            events = list(self._release_events)
        for event in events:
            event.set()

    def wait_for_calls(self, expected, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                calls = self.calls
            if calls >= expected:
                return True
            time.sleep(0.005)
        return False

    def wait_for_finished(self, expected, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                events = list(self._finished_events[:expected])
            if len(events) >= expected and all(event.is_set() for event in events):
                return True
            time.sleep(0.005)
        return False


class FakeKeyboard:
    """测试热键处理用的最小 msvcrt 替身。"""

    def __init__(self, keys):
        self.keys = list(keys)

    def kbhit(self):
        return bool(self.keys)

    def getwch(self):
        return self.keys.pop(0)


class FixedSmsParserTest(unittest.TestCase):
    """验证固定来源响应解析不会把空状态或日期当成验证码。"""

    def test_yuntl_empty_status_has_no_code(self):
        result = parse_fixed_sms_response("暂无短信|链接到期时间2026-06-30 12:34:56")

        self.assertFalse(result.has_sms)
        self.assertEqual(result.status, "暂无短信")
        self.assertEqual(result.code, "")

    def test_esim_empty_status_has_no_code(self):
        result = parse_fixed_sms_response("no sms-2026-06-30 12:34")

        self.assertFalse(result.has_sms)
        self.assertEqual(result.status, "no sms")

    def test_esim_expired_status_isrecognized(self):
        # eSIM88 号码过期返回纯文本"已过期"，应识别为过期状态而非"未发现验证码"
        result = parse_fixed_sms_response("已过期")

        self.assertFalse(result.has_sms)
        self.assertEqual(result.status, "已过期")
        self.assertEqual(result.code, "")

    def test_english_code_is_extracted(self):
        result = parse_fixed_sms_response("Your verification code is 123456")

        self.assertTrue(result.has_sms)
        self.assertEqual(result.code, "123456")

    def test_code_before_expiry_date_wins(self):
        result = parse_fixed_sms_response("验证码：654321，链接到期时间2026-06-30 12:34:56")

        self.assertTrue(result.has_sms)
        self.assertEqual(result.code, "654321")

    def test_us_country_code_is_split_from_local_number(self):
        result = split_us_phone("+15550123456")

        self.assertEqual(result.country_code, "+1")
        self.assertEqual(result.local_number, "5550123456")

    def test_normalize_phone_keeps_explicit_plus_one_country_code(self):
        # 显式 +1 的北美号码必须保留国家码，否则 split_us_phone 无法拆出本地号
        self.assertEqual(normalize_phone("+15550123456"), "+15550123456")
        self.assertEqual(normalize_phone("+1 (555) 012-3456"), "+15550123456")

    def test_normalize_phone_does_not_assume_bare_eleven_digit_is_us(self):
        # 裸 11 位（无 +）可能是其他国家的号码，不默认当作美国号拆分
        self.assertEqual(normalize_phone("15550123456"), "15550123456")

    def test_normalize_phone_keeps_non_us_digits(self):
        # 非 +1 的国家码只保留数字串，国家码信息不在导入层强制保留
        self.assertEqual(normalize_phone("+8613800138000"), "8613800138000")

    def test_normalize_phone_empty(self):
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone(None), "")

    def test_generic_fallback_can_be_disabled(self):
        # HTML 页面里的裸数字（如端口号）禁用兜底后不应被当成验证码
        text = "<html><body>service on port 8080</body></html>"

        self.assertTrue(parse_fixed_sms_response(text, allow_generic=True).has_sms)
        self.assertFalse(parse_fixed_sms_response(text, allow_generic=False).has_sms)

    def test_keyword_code_survives_disabled_generic(self):
        # 即便禁用兜底，含关键字的真实验证码仍能提取
        result = parse_fixed_sms_response(
            "<p>your code is 246810</p>", allow_generic=False
        )

        self.assertTrue(result.has_sms)
        self.assertEqual(result.code, "246810")

    def test_looks_like_html_detects_real_tags_only(self):
        from monitor import looks_like_html

        # 真实 HTML/XML 标签才算 html
        self.assertTrue(looks_like_html("<html><body>x</body></html>"))
        self.assertTrue(looks_like_html("<p>code</p>"))
        self.assertTrue(looks_like_html("</root>"))
        self.assertTrue(looks_like_html("<!-- comment -->"))
        # yuntl 把纯文本标成 text/html，但内容无标签，不应被当 html
        self.assertFalse(looks_like_html("暂无短信|链接到期时间2026-07-25 11:59:59"))
        self.assertFalse(looks_like_html("G-123456 is your Google verification code."))

    def test_fixed_source_extracts_google_code_mislabeled_as_html(self):
        """yuntl 把纯文本标成 text/html，Google 数字在前的验证码仍应提取。

        回归：修复前 Content-Type 含 html 即 allow_generic=False，CODE_PATTERNS
        只匹配关键字在前，Google 的 "G-123456 is your Google verification code"
        数字在前会被判"未发现验证码"，与"浏览器看得到码、监控收不到"现象一致。
        """
        source = FixedUrlSource(
            {"label": "yuntl", "phone": "15550123456", "url": "https://example.invalid/sms"},
            FixedReadySession(
                FakeTextResponse(
                    "G-123456 is your Google verification code.|链接到期时间2026-07-25 11:59:59",
                    content_type="text/html; charset=UTF-8",
                )
            ),
            request_timeout=1,
        )

        self.assertEqual(source.poll(), "123456")
        self.assertEqual(source.last_code, "123456")

    def test_fixed_source_strips_html_to_extract_code(self):
        """icloud-api.top 等网页接码：验证码埋在 HTML，关键字和数字间隔标签。

        回归：修复前 FixedUrlSource 直接在原始 HTML 上解析，"验证码"和 896973
        之间隔着 </p>、换行等标签（超 30 字符），CODE_PATTERNS 跨不过去漏掉
        验证码，与"浏览器看得到码、监控收不到"现象一致；去标签后纯文本即可命中。
        """
        html = (
            "<html><body>"
            "<p>您的临时验证码已寄出</p>"
            "<span>896973</span>"
            "</body></html>"
        )
        source = FixedUrlSource(
            {"label": "icloud-api", "phone": "4069018283", "url": "https://example.invalid/s"},
            FixedReadySession(FakeTextResponse(html, content_type="text/html; charset=utf-8")),
            request_timeout=1,
        )

        self.assertEqual(source.poll(), "896973")
        self.assertEqual(source.last_code, "896973")


class EmailSourceParserTest(unittest.TestCase):
    """验证邮箱正文（subject+body 合并）能提取验证码。"""

    def test_chatgpt_chinese_email_code_is_extracted(self):
        # 实测 email.nloop.cc 取回的 ChatGPT 中文验证码邮件正文
        text = "你的临时 ChatGPT 登录代码 输入此临时验证码以继续： 213787 未请求验证码？你可以忽略此邮件。"
        result = parse_fixed_sms_response(text, allow_generic=True)

        self.assertTrue(result.has_sms)
        self.assertEqual(result.code, "213787")

    def test_english_email_code_is_extracted(self):
        text = "Sign in to your account. Your verification code is 123456. Thanks."
        result = parse_fixed_sms_response(text, allow_generic=True)

        self.assertTrue(result.has_sms)
        self.assertEqual(result.code, "123456")

    def test_mail_id_change_does_not_repeat_same_code(self):
        class Session:
            def __init__(self):
                self.mail_id = "mail-1"

            def post(self, *args, **kwargs):
                return FakeJsonResponse(
                    {
                        "ok": True,
                        "mails": [
                            {
                                "id": self.mail_id,
                                "subject": "Verification code 123456",
                                "body": "code 123456",
                            }
                        ],
                    }
                )

        session = Session()
        source = EmailSource(
            {
                "label": "mail",
                "email": "a@example.com",
                "provider": "icloud",
                "base_url": "https://example.invalid",
            },
            session,
        )

        self.assertEqual(source.poll(), "123456")
        session.mail_id = "mail-2"
        self.assertIsNone(source.poll())
        self.assertEqual(len(source.history), 1)


class NormalizeEmailSourcesTest(unittest.TestCase):
    """验证 email_sources 配置校验：缺 email 退出、provider 缺省与限制。"""

    def test_missing_email_exits(self):
        with self.assertRaises(ConfigCommandError):
            normalize_email_sources([{"label": "iCloud"}])

    def test_provider_defaults_to_icloud_and_base_url_normalized(self):
        sources = normalize_email_sources(
            [{"email": "a@icloud.com", "base_url": "https://email.nloop.cc/"}]
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["provider"], "icloud")
        self.assertEqual(sources[0]["email"], "a@icloud.com")
        # 末尾斜杠应被去掉，避免拼接出 //api
        self.assertEqual(sources[0]["base_url"], "https://email.nloop.cc")

    def test_non_icloud_provider_exits(self):
        with self.assertRaises(ConfigCommandError):
            normalize_email_sources([{"email": "a@outlook.com", "provider": "outlook"}])

    def test_songniqu_derives_email_and_uses_provider_default(self):
        sources = normalize_email_sources(
            [{"label": "Mail", "provider": "songniqu", "mailbox": "user@example.com=SECRET_KEY"}]
        )

        self.assertEqual(sources[0]["email"], "user@example.com")
        self.assertEqual(sources[0]["mailbox"], "user@example.com=SECRET_KEY")
        self.assertEqual(sources[0]["base_url"], "https://mail.songniqu.cfd")

    def test_songniqu_rejects_bad_or_mismatched_mailbox_without_leaking(self):
        secret = "user@example.com=SECRET_KEY"
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(ConfigCommandError):
            normalize_email_sources(
                [
                    {
                        "provider": "songniqu",
                        "email": "other@example.com",
                        "mailbox": secret,
                    }
                ]
            )
        self.assertNotIn("SECRET_KEY", stdout.getvalue())

        with self.assertRaises(ConfigCommandError):
            normalize_email_sources([{"provider": "songniqu", "mailbox": "not-a-mailbox"}])


class TotpTest(unittest.TestCase):
    """验证标准库 TOTP 实现符合 RFC6238 SHA1 测试向量。"""

    def test_rfc6238_sha1_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

        self.assertEqual(generate_totp(secret, digits=8, timestamp=59), "94287082")

    def test_invalid_secret_raises_value_error(self):
        with self.assertRaises(ValueError):
            generate_totp("not-valid-base32")

    def test_remaining_is_inside_period(self):
        remaining = totp_remaining()

        self.assertGreaterEqual(remaining, 1)
        self.assertLessEqual(remaining, 30)


class NormalizeAccountsTest(unittest.TestCase):
    """验证账户档案配置校验不依赖真实密码或真实 2FA 密钥。"""

    def test_missing_login_email_exits(self):
        with self.assertRaises(ConfigCommandError):
            normalize_accounts([{"label": "ChatGPT"}])

    def test_defaults_and_secret_are_preserved(self):
        accounts = normalize_accounts(
            [{"login_email": "user@example.com", "totp_secret": "JBSWY3DPEHPK3PXP"}]
        )

        self.assertEqual(accounts[0]["label"], "账户1")
        self.assertEqual(accounts[0]["login_email"], "user@example.com")
        self.assertEqual(accounts[0]["password"], "")
        self.assertEqual(accounts[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(accounts[0]["phone"], "")
        self.assertEqual(accounts[0]["email"], "")


class AccountSourceTest(unittest.TestCase):
    """验证账户来源只暴露手动复制字段，不参与验证码自动轮询。"""

    def test_current_totp_returns_six_digits(self):
        account = AccountSource(
            {
                "label": "ChatGPT",
                "login_email": "user@example.com",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        )

        self.assertRegex(account.current_totp, r"^\d{6}$")

    def test_copy_fields_exposes_login_totp_and_config_phone_number(self):
        account = AccountSource(
            {
                "label": "ChatGPT",
                "login_email": "user@example.com",
                "password": "pw",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "phone": "+15550123456",
            }
        )

        with patch("monitor.generate_totp", return_value="123456"):
            self.assertEqual(
                account.copy_fields(),
                [
                    ("1", "登录邮箱", "user@example.com"),
                    ("2", "2FA 动态码", "123456"),
                    ("3", "手机号码", "5550123456"),
                ],
            )

    def test_copy_fields_prefers_linked_source_phone_and_never_lists_codes(self):
        account = AccountSource(
            {
                "label": "ChatGPT",
                "login_email": "user@example.com",
                "phone": "+15550123456",
            }
        )
        phone_source = FixedUrlSource(
            {"label": "YunTL", "phone": "+15550987654", "url": "https://example.invalid/sms"},
            FixedReadySession(FakeTextResponse("暂无短信")),
        )
        email_source = FakePollable("iCloudMail")
        phone_source.last_code = "111111"
        email_source.last_code = "222222"
        account.linked_phone_source = phone_source
        account.linked_email_source = email_source

        self.assertEqual(
            account.copy_fields(),
            [
                ("1", "登录邮箱", "user@example.com"),
                ("2", "手机号码", "5550987654"),
            ],
        )

    def test_copy_fields_skip_empty_linked_codes_and_missing_phone(self):
        account = AccountSource({"label": "ChatGPT", "login_email": "user@example.com"})
        account.linked_phone_source = FakePollable("YunTL")
        account.linked_email_source = FakePollable("iCloudMail")
        account.linked_phone_source.last_code = ""
        account.linked_email_source.last_code = ""

        self.assertEqual(account.copy_fields(), [("1", "登录邮箱", "user@example.com")])

    def test_email_source_poll_returns_new_code_for_auto_copy_chain(self):
        source = EmailSource(
            {
                "label": "iCloudMail",
                "email": "user@icloud.com",
                "provider": "icloud",
                "base_url": "https://email.nloop.cc",
            },
            EmailReadySession(
                FakeJsonResponse(
                    {
                        "ok": True,
                        "mails": [
                            {
                                "id": "mail-1",
                                "subject": "Sign in",
                                "body": "Your verification code is 246810.",
                            }
                        ],
                    }
                )
            ),
            request_timeout=1,
        )

        self.assertEqual(source.poll(), "246810")
        self.assertEqual(source.last_code, "246810")

    def test_songniqu_poll_prefers_top_level_code_and_redacts_history(self):
        mailbox = "user@example.com=SECRET_KEY"
        session = EmailReadySession(
            FakeJsonResponse(
                {
                    "ok": True,
                    "code": "135790",
                    "mails": [
                        {
                            "id": "mail-1",
                            "subject": f"Code for {mailbox}",
                            "body": "fallback 246810 SECRET_KEY",
                        }
                    ],
                }
            )
        )
        source = EmailSource(
            {
                "label": "Songniqu",
                "email": "user@example.com",
                "provider": "songniqu",
                "base_url": "https://mail.songniqu.cfd",
                "mailbox": mailbox,
            },
            session,
            request_timeout=1,
        )

        self.assertEqual(source.poll(), "135790")
        self.assertEqual(source.copy_number, "user@example.com")
        self.assertEqual(
            session.calls[0],
            (
                "https://mail.songniqu.cfd/api/receive",
                {"mailbox": mailbox, "turnstile_token": ""},
                1,
            ),
        )
        history = json.dumps(list(source.history), ensure_ascii=False)
        self.assertNotIn(mailbox, history)
        self.assertNotIn("SECRET_KEY", history)

    def test_songniqu_poll_falls_back_to_mail_code_then_body(self):
        base = {
            "label": "Songniqu",
            "email": "user@example.com",
            "provider": "songniqu",
            "base_url": "https://mail.songniqu.cfd",
            "mailbox": "user@example.com=SECRET_KEY",
        }
        direct = EmailSource(
            base,
            EmailReadySession(
                FakeJsonResponse(
                    {"ok": True, "mails": [{"id": "1", "verification_code": "112233"}]}
                )
            ),
        )
        parsed = EmailSource(
            base,
            EmailReadySession(
                FakeJsonResponse(
                    {
                        "ok": True,
                        "mails": [{"id": "2", "subject": "Sign in", "body": "Code: 445566"}],
                    }
                )
            ),
        )

        self.assertEqual(direct.poll(), "112233")
        self.assertEqual(parsed.poll(), "445566")

    def test_songniqu_empty_mailbox_is_ready_but_has_no_code(self):
        source = EmailSource(
            {
                "label": "Songniqu",
                "email": "user@example.com",
                "provider": "songniqu",
                "base_url": "https://mail.songniqu.cfd",
                "mailbox": "user@example.com=SECRET_KEY",
            },
            EmailReadySession(FakeJsonResponse({"ok": True, "mails": []})),
        )

        self.assertIsNone(source.poll())
        self.assertEqual(source.status, "暂无邮件")

    def test_songniqu_failure_states_are_fixed_and_secret_free(self):
        mailbox = "user@example.com=SECRET_KEY"
        config = {
            "label": "Songniqu",
            "email": "user@example.com",
            "provider": "songniqu",
            "base_url": "https://mail.songniqu.cfd",
            "mailbox": mailbox,
        }
        cases = [
            ({"ok": False, "code": "turnstile_failed", "message": mailbox}, "需要网页验证"),
            ({"ok": False, "code": "mailbox_bound", "credential": mailbox}, "需要网页绑定"),
            ({"ok": False, "message": mailbox}, "取件失败"),
        ]
        for payload, expected_status in cases:
            source = EmailSource(config, EmailReadySession(FakeJsonResponse(payload)))
            self.assertIsNone(source.poll())
            self.assertEqual(source.status, expected_status)
            output = f"{source.status} {source.note}"
            self.assertNotIn(mailbox, output)
            self.assertNotIn("SECRET_KEY", output)

    def test_poll_never_returns_code(self):
        account = AccountSource({"label": "iCloud", "login_email": "a@icloud.com"})

        self.assertIsNone(account.poll())
        self.assertEqual(account.copy_number, "a@icloud.com")

    def test_monitor_places_accounts_before_unlinked_sources(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [{"label": "A", "login_email": "a@example.com"}],
            }
        )

        self.assertEqual([source.label for source in monitor.sources], ["A", "LuDan"])
        self.assertTrue(getattr(monitor.sources[0], "is_account"))

    def test_monitor_hides_sources_linked_to_accounts_but_keeps_polling(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [
                    {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/y"},
                    {"label": "eSIM88", "phone": "15550987654", "url": "https://example.invalid/e"},
                    {"label": "ka001", "phone": "15550112233", "url": "https://example.invalid/k"},
                ],
                "email_sources": [
                    {
                        "label": "iCloudMail",
                        "provider": "icloud",
                        "base_url": "https://email.nloop.cc",
                        "email": "a@icloud.com",
                    }
                ],
                "accounts": [
                    {"label": "ChatGPT", "login_email": "g@example.com", "phone": "15550123456"},
                    {
                        "label": "iCloudAccount",
                        "login_email": "a@icloud.com",
                        "phone": "15550112233",
                        "email": "a@icloud.com",
                    },
                ],
            }
        )

        self.assertEqual(
            [source.label for source in monitor.sources],
            ["ChatGPT", "iCloudAccount", "LuDan", "eSIM88"],
        )
        self.assertEqual(
            [source.label for source in monitor.pollables],
            ["LuDan", "YunTL", "eSIM88", "ka001", "iCloudMail"],
        )
        self.assertEqual(monitor.accounts[0].linked_phone_source.label, "YunTL")
        self.assertEqual(monitor.accounts[1].linked_phone_source.label, "ka001")
        self.assertEqual(monitor.accounts[1].linked_email_source.label, "iCloudMail")

    def test_render_account_never_prints_password(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [
                    {"label": "A", "login_email": "a@example.com", "password": "SECRET_PASSWORD"}
                ],
            }
        )
        output = io.StringIO()

        with redirect_stdout(output):
            monitor.render_account_source(1, monitor.accounts[0])

        self.assertIn("密码：已配置", output.getvalue())
        self.assertNotIn("SECRET_PASSWORD", output.getvalue())

    def test_render_and_copy_show_songniqu_email_but_never_mailbox_key(self):
        mailbox = "receive@example.com=SECRET_KEY"
        monitor_instance = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "",
                "fixed_sources": [],
                "email_sources": [
                    {
                        "label": "Songniqu",
                        "provider": "songniqu",
                        "base_url": "https://mail.songniqu.cfd",
                        "email": "receive@example.com",
                        "mailbox": mailbox,
                    }
                ],
                "accounts": [
                    {
                        "label": "A",
                        "login_email": "82@example.com",
                        "email": "receive@example.com",
                    }
                ],
            }
        )
        output = io.StringIO()

        with redirect_stdout(output), patch("monitor.clear_screen"):
            monitor_instance.render()

        rendered = output.getvalue()
        self.assertIn("取件邮箱：receive@example.com", rendered)
        self.assertEqual(monitor_instance.email_sources[0].copy_number, "receive@example.com")
        self.assertNotIn(mailbox, rendered)
        self.assertNotIn("SECRET_KEY", rendered)

    def test_disable_runtime_unlinks_account_immediately(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "",
                "fixed_sources": [
                    {"label": "SMS", "phone": "+15550123456", "url": "https://example.invalid/sms"}
                ],
                "email_sources": [],
                "accounts": [
                    {
                        "label": "A",
                        "login_email": "a@example.com",
                        "phone": "+15550123456",
                        "phone_source_label": "SMS",
                    }
                ],
            }
        )

        with patch("monitor.disable_source"):
            monitor._disable_runtime("fixed", "SMS", "认证失败")

        self.assertIsNone(monitor.accounts[0].linked_phone_source)
        self.assertNotIn("SMS", [source.label for source in monitor.pollables])

    def test_account_can_link_dynamic_phone_source_by_label(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "",
                "fixed_sources": [],
                "email_sources": [],
                "kkdos_sources": [{"label": "kkdos", "cdk": "SECRET_CDK"}],
                "accounts": [
                    {
                        "label": "sk7398965",
                        "login_email": "sk7398965@example.com",
                        "phone": "2087605936",
                        "phone_source_label": "kkdos",
                    }
                ],
            }
        )

        self.assertEqual([source.label for source in monitor.sources], ["sk7398965"])
        self.assertEqual([source.label for source in monitor.pollables], ["kkdos"])
        self.assertEqual(monitor.accounts[0].linked_phone_source.label, "kkdos")


class RefreshModeTest(unittest.TestCase):
    """验证默认低频刷新、复制后高频等码，拿到验证码即停高频。"""

    def test_request_timeout_config_is_passed_to_sources(self):
        session = TimeoutRecordingSession()
        timeout = 1.5
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "request_timeout": timeout,
                "fixed_sources": [
                    {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/y"}
                ],
                "email_sources": [
                    {
                        "label": "iCloud",
                        "provider": "icloud",
                        "base_url": "https://email.nloop.cc",
                        "email": "a@icloud.com",
                    }
                ],
                "accounts": [],
            }
        )
        for source in monitor.pollables:
            source.session = session

        for source in monitor.pollables:
            source.poll()

        self.assertEqual(session.get_timeouts, [timeout, timeout])
        self.assertEqual(session.post_timeouts, [timeout])

    def test_poll_sources_collects_fast_result_when_one_source_is_slow(self):
        monitor = SmsMonitor.__new__(SmsMonitor)
        fast = FakePollable("Fast", code="111111")
        slow = FakePollable("Slow", code="222222", delay=0.2)
        monitor.pollables = [fast, slow]
        monitor.max_poll_workers = 2
        monitor.poll_round_timeout = 0.05

        self.assertTrue(hasattr(monitor, "poll_sources"), "SmsMonitor.poll_sources should exist")
        started = time.monotonic()
        new_codes = monitor.poll_sources()
        elapsed = time.monotonic() - started

        self.assertEqual(new_codes, [("Fast", "111111")])
        self.assertLess(elapsed, 0.15)

    def test_poll_sources_skips_source_whose_poll_raises(self):
        # 某 source.poll() 漏网抛异常时，poll_sources 不应崩溃，异常来源跳过本轮，
        # 其他来源的新码照常收集。
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor._pending_polls = []

        class RaisingPollable(FakePollable):
            def poll(self):
                raise RuntimeError("boom")

        bad = RaisingPollable("Bad", code="999999")
        good = FakePollable("Good", code="111111")
        monitor.pollables = [bad, good]
        monitor.max_poll_workers = 2
        monitor.poll_round_timeout = 1.0

        new_codes = monitor.poll_sources()

        self.assertEqual(new_codes, [("Good", "111111")])
        self.assertIn("轮询异常", bad.note)

    def test_poll_sources_marks_timeout_without_losing_existing_state(self):
        monitor = SmsMonitor.__new__(SmsMonitor)
        slow = FakePollable("Slow", code="222222", delay=0.2)
        original_history = list(slow.history)
        monitor.pollables = [slow]
        monitor.max_poll_workers = 1
        monitor.poll_round_timeout = 0.05

        self.assertEqual(monitor.poll_sources(), [])

        self.assertEqual(slow.last_code, "OLD")
        self.assertEqual(slow.history, original_history)
        self.assertIn("超时", slow.note)
        self.assertEqual(slow.status, "轮询超时")

    def test_poll_sources_copies_late_code_on_next_round(self):
        """超时 worker 迟到写入的新码必须在下一轮被补复制，不能丢失。"""

        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor._pending_polls = []
        slow = GatedStatefulPollable("Slow", code="555555")
        monitor.pollables = [slow]
        monitor.max_poll_workers = 1
        monitor.poll_round_timeout = 0.05

        try:
            # 第一轮：慢来源超时，未拿到码
            self.assertEqual(monitor.poll_sources(), [])
            self.assertIn("超时", slow.note)

            # 释放迟到 worker，让它真实完成并写入 last_code/history。
            slow.release_all()
            self.assertTrue(slow.wait_for_finished(1))

            # 第二轮：结算上一轮遗留的迟到 worker，新码应被补复制。
            # 若无此机制，迟到 worker 已把 last_code 写成 555555，本轮 fresh poll
            # 会判定为非新码返回 None，验证码将永久漏复制。
            second_round = monitor.poll_sources()
            self.assertEqual(second_round, [("Slow", "555555")])
            self.assertEqual(slow.calls, 1)
            self.assertEqual(monitor._pending_polls, [])
        finally:
            slow.release_all()

    def test_poll_sources_keeps_still_pending_worker_for_later_reconcile(self):
        """连续多轮仍未完成的超时 worker 也必须保留到完成后再补复制。"""

        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor._pending_polls = []
        slow = GatedStatefulPollable("Slow", code="666666")
        monitor.pollables = [slow]
        monitor.max_poll_workers = 1
        monitor.poll_round_timeout = 0.05

        try:
            self.assertEqual(monitor.poll_sources(), [])
            self.assertEqual(monitor.poll_sources(), [])
            self.assertEqual(slow.calls, 1)

            slow.release_all()
            self.assertTrue(slow.wait_for_finished(1))

            self.assertEqual(monitor.poll_sources(), [("Slow", "666666")])
            self.assertEqual(slow.calls, 1)
            self.assertEqual(monitor._pending_polls, [])
        finally:
            slow.release_all()

    def test_poll_sources_does_not_rollback_not_done_worker_state(self):
        """仍在运行的 worker 不应被回滚，避免与 worker 并发写 history 互相踩踏。"""

        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor._pending_polls = []
        slow = GatedStatefulPollable("Slow", code="777777", write_before_release=True)
        monitor.pollables = [slow]
        monitor.max_poll_workers = 1
        monitor.poll_round_timeout = 0.05

        try:
            self.assertEqual(monitor.poll_sources(), [])
            self.assertTrue(slow.wait_for_calls(1))
            self.assertEqual(slow.last_code, "777777")

            self.assertEqual(monitor.poll_sources(), [])
            self.assertEqual(slow.last_code, "777777")
            self.assertEqual(slow.history[0][1], "777777")
        finally:
            slow.release_all()
            slow.wait_for_finished(1)

    def test_poll_sources_retries_after_pending_round_limit(self):
        """永久不返回的来源达到挂起轮次上限后，应允许 fresh poll 重试。"""

        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor._pending_polls = []
        monitor.pending_poll_max_rounds = 1
        slow = GatedStatefulPollable("Slow")
        original_session = object()
        slow.session = original_session
        monitor.pollables = [slow]
        monitor.max_poll_workers = 1
        monitor.poll_round_timeout = 0.05

        try:
            self.assertEqual(monitor.poll_sources(), [])
            self.assertTrue(slow.wait_for_calls(1))

            self.assertEqual(monitor.poll_sources(), [])
            self.assertTrue(slow.wait_for_calls(2))
            self.assertIsNot(slow.session, original_session)
        finally:
            slow.release_all()
            slow.wait_for_finished(2)
            monitor._reconcile_pending_polls()

    def test_refresh_mode_defaults_and_active_window(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [],
            }
        )

        self.assertEqual(monitor.idle_poll_interval, 15)
        self.assertEqual(monitor.active_after_copy_seconds, 180)
        self.assertTrue(monitor.active_until_code)
        self.assertFalse(monitor.active_waiting_for_code)
        self.assertEqual(monitor.current_poll_interval(now=100), 15)

        monitor.activate_high_frequency(now=100)

        self.assertTrue(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)
        self.assertTrue(monitor.is_active_mode(now=999))
        self.assertEqual(monitor.current_poll_interval(now=999), 5)

    def test_refresh_mode_honors_timeout_config_when_until_code_disabled(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "poll_interval": 7,
                "idle_poll_interval": 30,
                "active_after_copy_seconds": 60,
                "active_until_code": False,
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [],
            }
        )

        self.assertEqual(monitor.poll_interval, 7)
        self.assertEqual(monitor.idle_poll_interval, 30)
        self.assertFalse(monitor.active_until_code)
        monitor.activate_high_frequency(now=10)
        self.assertEqual(monitor.active_until, 70)
        self.assertFalse(monitor.active_waiting_for_code)
        self.assertEqual(monitor.current_poll_interval(now=20), 7)

    def test_regular_source_copy_success_enters_active_mode(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [
                    {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/y"}
                ],
                "email_sources": [],
                "accounts": [],
            }
        )

        with patch("monitor.copy_to_clipboard", return_value=True), patch("monitor.time.time", return_value=100):
            monitor.copy_source_number(2)

        self.assertTrue(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)
        self.assertIn("已复制", monitor.note)

    def test_regular_source_copy_failure_stays_idle(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [
                    {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/y"}
                ],
                "email_sources": [],
                "accounts": [],
            }
        )

        with patch("monitor.copy_to_clipboard", return_value=False), patch("monitor.time.time", return_value=100):
            monitor.copy_source_number(2)

        self.assertFalse(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)
        self.assertEqual(monitor.note, "复制失败")

    def test_account_copy_success_enters_active_mode(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [{"label": "A", "login_email": "a@example.com"}],
            }
        )

        with (
            patch("monitor.msvcrt", None),
            patch("monitor.copy_to_clipboard", return_value=True),
            patch("monitor.time.time", return_value=100),
        ):
            monitor.copy_source_number(1)

        self.assertTrue(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)
        self.assertIn("已复制", monitor.note)

    def test_account_copy_menu_copies_current_totp_by_number(self):
        copied = []
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [
                    {
                        "label": "A",
                        "login_email": "a@example.com",
                        "password": "pw",
                        "totp_secret": "JBSWY3DPEHPK3PXP",
                    }
                ],
            }
        )

        with (
            patch("monitor.generate_totp", return_value="123456"),
            patch("monitor.msvcrt", FakeKeyboard(["2"])),
            patch("monitor.copy_to_clipboard", side_effect=lambda value: copied.append(value) or True),
            patch("monitor.time.time", return_value=100),
            patch.object(monitor, "render"),
            patch.object(monitor, "render_copy_menu"),
        ):
            monitor.copy_source_number(1)

        self.assertEqual(copied, ["123456"])
        self.assertTrue(monitor.active_waiting_for_code)
        self.assertIn("2FA 动态码", monitor.note)

    def test_account_copy_cancel_stays_idle(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [{"label": "A", "login_email": "a@example.com"}],
            }
        )

        with (
            patch("monitor.msvcrt", FakeKeyboard(["\x1b"])),
            patch.object(monitor, "render"),
            patch.object(monitor, "render_copy_menu"),
        ):
            monitor.copy_source_number(1)

        self.assertEqual(monitor.active_until, 0)
        self.assertIn("已取消", monitor.note)

    def test_ludan_manual_change_enters_active_mode(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [],
            }
        )

        with (
            patch("monitor.msvcrt", FakeKeyboard(["n"])),
            patch("monitor.time.time", return_value=100),
            patch.object(monitor, "render"),
            patch.object(monitor.ludan, "change_number"),
        ):
            self.assertTrue(monitor.handle_keys())

        self.assertTrue(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)

    def test_auto_copy_new_code_stops_high_frequency(self):
        copied = []
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor.note = ""
        monitor.active_until_code = True
        monitor.active_waiting_for_code = True
        monitor.active_until = 999

        def fake_copy(text):
            copied.append(text)
            return True

        monitor.auto_copy_codes([("LuDan", "111111")], fake_copy)

        self.assertEqual(copied, ["111111"])
        self.assertFalse(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)

    def test_auto_copy_keeps_timed_high_frequency_window(self):
        """定时高频模式（active_until_code=False）拿到码不应提前清零高频窗口。"""

        copied = []
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor.note = ""
        monitor.active_until_code = False
        monitor.active_waiting_for_code = False
        monitor.active_until = 999

        def fake_copy(text):
            copied.append(text)
            return True

        monitor.auto_copy_codes([("LuDan", "111111")], fake_copy)

        self.assertEqual(copied, ["111111"])
        # 定时高频窗口必须保留，不能被 deactivate_high_frequency 清零
        self.assertEqual(monitor.active_until, 999)
        self.assertFalse(monitor.active_waiting_for_code)

    def test_run_uses_poll_sources_before_auto_copy_codes(self):
        events = []
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor.pollables = []
        monitor.accounts = []

        class FakeLuDan:
            def verify(self):
                events.append("verify")

            def refresh_number(self):
                events.append("refresh_number")

        monitor.ludan = FakeLuDan()

        def poll_sources():
            events.append("poll_sources")
            return [("Fast", "111111")]

        def auto_copy_codes(pairs):
            events.append(("auto_copy_codes", pairs))

        def render():
            events.append("render")

        def current_poll_interval():
            events.append("current_poll_interval")
            return 1

        def handle_keys():
            events.append("handle_keys")
            return False

        monitor.poll_sources = poll_sources
        monitor.auto_copy_codes = auto_copy_codes
        monitor.render = render
        monitor.current_poll_interval = current_poll_interval
        monitor.handle_keys = handle_keys
        monitor.is_active_mode = lambda: False

        with redirect_stdout(io.StringIO()):
            monitor.run()

        self.assertEqual(
            events[:5],
            [
                "verify",
                "refresh_number",
                "poll_sources",
                ("auto_copy_codes", [("Fast", "111111")]),
                "render",
            ],
        )


class ConfigCommandTest(unittest.TestCase):
    """验证 Codex/Claude Code 可调用的标准配置录入命令。"""

    def test_init_and_set_global_write_config_without_leaking_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            env = {"SMS_MONITOR_KEY": "REAL_SECRET_KEY"}

            init_result = run_config_command(["init", "--config", config_path, "--json"], env=env)
            result = run_config_command(
                [
                    "set-global",
                    "--config",
                    config_path,
                    "--base-url",
                    "https://example.invalid/api",
                    "--key-env",
                    "SMS_MONITOR_KEY",
                    "--poll-interval",
                    "7",
                    "--idle-poll-interval",
                    "21",
                    "--request-timeout",
                    "2.5",
                    "--max-poll-workers",
                    "3",
                    "--poll-round-timeout",
                    "3.0",
                    "--active-until-code",
                    "false",
                    "--active-after-copy-seconds",
                    "90",
                    "--auto-change-on-expire",
                    "false",
                    "--json",
                ],
                env=env,
            )

            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.assertTrue(init_result["ready"])
            self.assertTrue(result["ready"])
            self.assertEqual(cfg["base_url"], "https://example.invalid/api")
            self.assertEqual(cfg["key"], "REAL_SECRET_KEY")
            self.assertEqual(cfg["poll_interval"], 7)
            self.assertEqual(cfg["idle_poll_interval"], 21)
            self.assertEqual(cfg["request_timeout"], 2.5)
            self.assertEqual(cfg["max_poll_workers"], 3)
            self.assertEqual(cfg["poll_round_timeout"], 3.0)
            self.assertFalse(cfg["active_until_code"])
            self.assertEqual(cfg["active_after_copy_seconds"], 90)
            self.assertFalse(cfg["auto_change_on_expire"])
            self.assertNotIn("REAL_SECRET_KEY", json.dumps(result, ensure_ascii=False))

    def test_upsert_sources_and_account_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            env = {
                "FIXED_URL": "https://example.invalid/sms?token=SECRET_TOKEN",
                "KKDOS_CDK": "SECRET_KKDOS_CDK",
                "ACCOUNT_PASSWORD": "SECRET_PASSWORD",
                "ACCOUNT_TOTP": "JBSWY3DPEHPK3PXP",
            }

            run_config_command(["init", "--config", config_path], env=env)
            run_config_command(
                [
                    "upsert-fixed",
                    "--config",
                    config_path,
                    "--label",
                    "YunTL",
                    "--phone",
                    "15550123456",
                    "--url-env",
                    "FIXED_URL",
                ],
                env=env,
            )
            run_config_command(
                [
                    "upsert-fixed",
                    "--config",
                    config_path,
                    "--label",
                    "YunTL",
                    "--phone",
                    "15550999999",
                    "--url-env",
                    "FIXED_URL",
                ],
                env=env,
            )
            run_config_command(
                [
                    "upsert-kkdos",
                    "--config",
                    config_path,
                    "--label",
                    "kkdos",
                    "--cdk-env",
                    "KKDOS_CDK",
                ],
                env=env,
            )
            run_config_command(
                [
                    "upsert-email",
                    "--config",
                    config_path,
                    "--label",
                    "iCloud",
                    "--email",
                    "user@icloud.com",
                    "--provider",
                    "icloud",
                    "--base-url",
                    "https://email.nloop.cc",
                ],
                env=env,
            )
            account_result = run_config_command(
                [
                    "upsert-account",
                    "--config",
                    config_path,
                    "--label",
                    "ChatGPT",
                    "--login-email",
                    "login@example.com",
                    "--password-env",
                    "ACCOUNT_PASSWORD",
                    "--totp-secret-env",
                    "ACCOUNT_TOTP",
                    "--phone",
                    "15550999999",
                    "--email",
                    "user@icloud.com",
                    "--phone-source-label",
                    "kkdos",
                    "--note",
                    "primary",
                    "--json",
                ],
                env=env,
            )

            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.assertEqual(len(cfg["fixed_sources"]), 1)
            self.assertEqual(cfg["fixed_sources"][0]["phone"], "15550999999")
            self.assertEqual(len(cfg["kkdos_sources"]), 1)
            self.assertEqual(cfg["kkdos_sources"][0]["label"], "kkdos")
            self.assertEqual(cfg["kkdos_sources"][0]["cdk"], "SECRET_KKDOS_CDK")
            self.assertEqual(len(cfg["email_sources"]), 1)
            self.assertEqual(cfg["email_sources"][0]["email"], "user@icloud.com")
            self.assertEqual(len(cfg["accounts"]), 1)
            self.assertEqual(cfg["accounts"][0]["phone"], "15550999999")
            self.assertEqual(cfg["accounts"][0]["phone_source_label"], "kkdos")
            self.assertEqual(cfg["accounts"][0]["email"], "user@icloud.com")
            self.assertEqual(cfg["accounts"][0]["password"], "SECRET_PASSWORD")
            self.assertEqual(cfg["accounts"][0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            sanitized = json.dumps(account_result, ensure_ascii=False)
            self.assertNotIn("SECRET_PASSWORD", sanitized)
            self.assertNotIn("JBSWY3DPEHPK3PXP", sanitized)
            self.assertNotIn("SECRET_KKDOS_CDK", sanitized)

    def test_normalize_kkdos_sources_requires_cdk_without_leaking_value(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(ConfigCommandError):
            normalize_kkdos_sources([{"label": "kkdos", "cdk": ""}])
        self.assertNotIn("SECRET_CDK", stdout.getvalue())

        source = normalize_kkdos_sources(
            [{"label": "kkdos", "cdk": "SECRET_CDK", "base_url": "https://sms.kkdos.store/"}]
        )[0]
        self.assertEqual(source["label"], "kkdos")
        self.assertEqual(source["cdk"], "SECRET_CDK")
        self.assertEqual(source["base_url"], "https://sms.kkdos.store")

    def test_cli_validate_and_ready_check_json_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            env = {
                "SMS_MONITOR_KEY": "REAL_SECRET_KEY",
                "FIXED_URL": "https://example.invalid/sms?token=SECRET_TOKEN",
                "KKDOS_CDK": "SECRET_KKDOS_CDK",
                "ACCOUNT_PASSWORD": "SECRET_PASSWORD",
                "ACCOUNT_TOTP": "JBSWY3DPEHPK3PXP",
            }
            run_config_command(["init", "--config", config_path], env=env)
            run_config_command(
                ["set-global", "--config", config_path, "--key-env", "SMS_MONITOR_KEY"],
                env=env,
            )
            run_config_command(
                [
                    "upsert-fixed",
                    "--config",
                    config_path,
                    "--label",
                    "YunTL",
                    "--phone",
                    "15550123456",
                    "--url-env",
                    "FIXED_URL",
                ],
                env=env,
            )
            run_config_command(
                [
                    "upsert-kkdos",
                    "--config",
                    config_path,
                    "--label",
                    "kkdos",
                    "--cdk-env",
                    "KKDOS_CDK",
                ],
                env=env,
            )
            run_config_command(
                [
                    "upsert-account",
                    "--config",
                    config_path,
                    "--label",
                    "ChatGPT",
                    "--login-email",
                    "login@example.com",
                    "--password-env",
                    "ACCOUNT_PASSWORD",
                    "--totp-secret-env",
                    "ACCOUNT_TOTP",
                    "--phone",
                    "15550123456",
                ],
                env=env,
            )

            validate_stdout = io.StringIO()
            with redirect_stdout(validate_stdout):
                validate_code = run_cli(["config", "validate", "--config", config_path, "--json"], env=env)

            ready_stdout = io.StringIO()
            ready_sessions = iter(
                [
                    ActionSession(
                        {
                            "verify": [
                                {
                                    "code": 0,
                                    "data": {
                                        "phone": "15550123456",
                                        "remaining_seconds": 120,
                                    },
                                }
                            ]
                        }
                    ),
                    FixedReadySession(FakeTextResponse("暂无短信")),
                    KkdosFakeSession(
                        verify={
                            "success": True,
                            "data": {"sessionId": "sess-123", "phone": "+15550123456"},
                        }
                    ),
                ]
            )
            with redirect_stdout(ready_stdout):
                ready_code = run_cli(
                    ["config", "ready-check", "--config", config_path, "--all", "--json"],
                    env=env,
                    session_factory=lambda: next(ready_sessions),
                )

            self.assertEqual(validate_code, 0)
            self.assertEqual(ready_code, 0)
            validate_payload = json.loads(validate_stdout.getvalue())
            ready_payload = json.loads(ready_stdout.getvalue())
            self.assertTrue(validate_payload["ready"])
            self.assertTrue(ready_payload["ready"])
            self.assertIn("kkdos", [item["label"] for item in ready_payload["items"]])
            output = validate_stdout.getvalue() + ready_stdout.getvalue()
            for secret in [
                "REAL_SECRET_KEY",
                "SECRET_TOKEN",
                "SECRET_KKDOS_CDK",
                "SECRET_PASSWORD",
                "JBSWY3DPEHPK3PXP",
            ]:
                self.assertNotIn(secret, output)

    def test_cli_validate_invalid_config_still_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"fixed_sources": "not-an-array"}, f)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run_cli(
                    ["config", "validate", "--config", config_path, "--json"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "error")

    def test_parse_freeform_account_text_handles_messy_dash_format(self):
        raw = (
            "login@example.com----SECRET_PASSWORD---JBSWY3DPEHPK3PXP"
            "----+15550123456----https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN"
        )

        parsed = parse_freeform_account_text(raw, label="ChatGPT")

        self.assertEqual(parsed["label"], "ChatGPT")
        self.assertEqual(parsed["login_email"], "login@example.com")
        self.assertEqual(parsed["password"], "SECRET_PASSWORD")
        self.assertEqual(parsed["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["phone"], "+15550123456")
        self.assertEqual(parsed["sms_url"], "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN")
        preview = json.dumps(parsed["preview"], ensure_ascii=False)
        self.assertNotIn("SECRET_PASSWORD", preview)
        self.assertNotIn("JBSWY3DPEHPK3PXP", preview)
        self.assertNotIn("SECRET_TOKEN", preview)

    def test_parse_freeform_accounts_handles_chinese_multiline_batch(self):
        raw = """名称：账户A
邮箱：a@example.com
密码：SECRET_A
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/a?token=TOKEN_A

label: Account-B
email: b@example.com
password: SECRET_B
totp: JBSWY3DPEHPK3PXP
phone: +15550987654
url: https://sms.example/b?token=TOKEN_B"""

        items = parse_freeform_accounts(raw)

        self.assertEqual([item["label"] for item in items], ["账户A", "Account-B"])
        self.assertTrue(all(not item["missing"] for item in items))
        preview = json.dumps([item["preview"] for item in items], ensure_ascii=False)
        for secret in ("SECRET_A", "SECRET_B", "TOKEN_A", "TOKEN_B"):
            self.assertNotIn(secret, preview)

    def test_parse_freeform_accounts_handles_inline_natural_language(self):
        raw = (
            "ChatGPT谷歌邮箱：discarded@example.com ChatGPT密码：SECRET_PASSWORD "
            "一次性安全码密钥：JBSWY3DPEHPK3PXP 一次性安全码获取地址：2fa.example "
            "二验手机号：+15550123456 "
            "二验手机号验证码获取链接：https://sms.example/query?token=SECRET_TOKEN"
        )

        items = parse_freeform_accounts(raw)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["label"], "discarded")
        self.assertEqual(items[0]["login_email"], "discarded@example.com")
        self.assertEqual(items[0]["password"], "SECRET_PASSWORD")
        self.assertEqual(items[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(items[0]["phone"], "+15550123456")
        self.assertEqual(items[0]["sms_url"], "https://sms.example/query?token=SECRET_TOKEN")
        self.assertNotIn("2fa.example", json.dumps(items[0]["preview"], ensure_ascii=False))

    def test_private_template_has_requested_blank_slots(self):
        text = private_template_text(2)

        self.assertEqual(text.count("名称："), 2)
        self.assertEqual(text.count("接码链接："), 2)
        self.assertNotIn("example.com", text)
        self.assertNotIn("YOUR_", text)

    def test_private_template_command_creates_only_blank_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            private_path = os.path.join(tmp, "private-import.txt")

            result = run_config_command(
                ["private-template", "--count", "3", "--file", private_path, "--json"]
            )
            with open(private_path, "r", encoding="utf-8") as f:
                content = f.read()

        self.assertTrue(result["ready"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(content, private_template_text(3))

    def test_private_template_refuses_to_overwrite_filled_content(self):
        raw = "名称：Important\n邮箱：important@example.com\n密码：SECRET_PASSWORD\n"
        with tempfile.TemporaryDirectory() as tmp:
            private_path = os.path.join(tmp, "private-import.txt")
            with open(private_path, "w", encoding="utf-8") as f:
                f.write(raw)

            with self.assertRaises(ConfigCommandError):
                run_config_command(
                    ["private-template", "--count", "2", "--file", private_path, "--json"]
                )
            with open(private_path, "r", encoding="utf-8") as f:
                preserved = f.read()

        self.assertEqual(preserved, raw)

    def test_private_preview_does_not_write_config_and_success_clears_template(self):
        raw = """名称：Important-A
邮箱：important@example.com
密码：SECRET_PASSWORD
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/query?token=SECRET_TOKEN
"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            private_path = os.path.join(tmp, "private-import.txt")
            with open(private_path, "w", encoding="utf-8") as f:
                f.write(raw)

            preview = run_config_command(
                [
                    "import-private",
                    "--config",
                    config_path,
                    "--file",
                    private_path,
                    "--json",
                ]
            )
            self.assertFalse(os.path.exists(config_path))
            with open(private_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), raw)

            result = run_config_command(
                [
                    "import-private",
                    "--config",
                    config_path,
                    "--file",
                    private_path,
                    "--yes",
                    "--json",
                ]
            )
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            with open(private_path, "r", encoding="utf-8") as f:
                cleared = f.read()

        self.assertEqual(preview["status"], "needs_confirmation")
        self.assertTrue(result["ready"])
        self.assertEqual(cfg["accounts"][0]["phone_source_label"], "Important-A-SMS")
        self.assertEqual(cleared, private_template_text(1))
        sanitized = json.dumps(preview, ensure_ascii=False) + json.dumps(result, ensure_ascii=False)
        for secret in ("SECRET_PASSWORD", "JBSWY3DPEHPK3PXP", "SECRET_TOKEN"):
            self.assertNotIn(secret, sanitized)

    def test_private_invalid_batch_preserves_template(self):
        raw = "名称：Incomplete\n邮箱：incomplete@example.com\n密码：SECRET_PASSWORD\n"
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            private_path = os.path.join(tmp, "private-import.txt")
            with open(private_path, "w", encoding="utf-8") as f:
                f.write(raw)

            result = run_config_command(
                [
                    "import-private",
                    "--config",
                    config_path,
                    "--file",
                    private_path,
                    "--yes",
                    "--json",
                ]
            )
            with open(private_path, "r", encoding="utf-8") as f:
                preserved = f.read()

        self.assertEqual(result["status"], "invalid_batch")
        self.assertFalse(os.path.exists(config_path))
        self.assertEqual(preserved, raw)

    def test_private_cleanup_failure_reports_partial_success(self):
        raw = """名称：Important-A
邮箱：important@example.com
密码：SECRET_PASSWORD
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/query?token=SECRET_TOKEN
"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            private_path = os.path.join(tmp, "private-import.txt")
            with open(private_path, "w", encoding="utf-8") as f:
                f.write(raw)

            with patch("monitor.write_text_atomic", side_effect=ConfigCommandError("清空失败")):
                result = run_config_command(
                    [
                        "import-private",
                        "--config",
                        config_path,
                        "--file",
                        private_path,
                        "--yes",
                        "--json",
                    ]
                )
            config_written = os.path.exists(config_path)

        self.assertEqual(result["status"], "imported_cleanup_failed")
        self.assertTrue(config_written)
        self.assertFalse(result["ready"])

    def test_private_cleanup_failure_returns_nonzero_cli_status(self):
        raw = """名称：Important-A
邮箱：important@example.com
密码：SECRET_PASSWORD
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/query?token=SECRET_TOKEN
"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            private_path = os.path.join(tmp, "private-import.txt")
            with open(private_path, "w", encoding="utf-8") as f:
                f.write(raw)
            stdout = io.StringIO()

            with (
                patch("monitor.write_text_atomic", side_effect=ConfigCommandError("清空失败")),
                redirect_stdout(stdout),
            ):
                exit_code = run_cli(
                    [
                        "config",
                        "import-private",
                        "--config",
                        config_path,
                        "--file",
                        private_path,
                        "--yes",
                        "--json",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "imported_cleanup_failed")
        self.assertNotIn("SECRET_PASSWORD", stdout.getvalue())

    def test_batch_import_is_atomic_and_reports_email_conflict(self):
        initial = """名称：Existing
邮箱：same@example.com
密码：SECRET_A
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/a?token=TOKEN_A"""
        conflict = """名称：Different
邮箱：same@example.com
密码：SECRET_B
2FA：JBSWY3DPEHPK3PXP
手机号：+15550987654
接码链接：https://sms.example/b?token=TOKEN_B"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            run_config_command(["init", "--config", config_path])
            run_config_command(
                ["import-freeform", "--config", config_path, "--stdin", "--yes"],
                input_reader=lambda: initial,
            )
            with open(config_path, "r", encoding="utf-8") as f:
                before = f.read()

            result = run_config_command(
                ["import-freeform", "--config", config_path, "--stdin", "--yes", "--json"],
                input_reader=lambda: conflict,
            )
            with open(config_path, "r", encoding="utf-8") as f:
                after = f.read()

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "invalid_batch")
        self.assertTrue(result["conflicts"])
        self.assertEqual(before, after)

    def test_batch_preview_and_write_include_explicit_source_links(self):
        raw = """名称：A
邮箱：a@example.com
密码：SECRET_A
2FA：JBSWY3DPEHPK3PXP
手机号：+15550123456
接码链接：https://sms.example/a?token=TOKEN_A

名称：B
邮箱：b@example.com
密码：SECRET_B
2FA：JBSWY3DPEHPK3PXP
手机号：+15550987654
接码链接：https://sms.example/b?token=TOKEN_B"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            preview = run_config_command(
                ["import-freeform", "--config", config_path, "--stdin", "--json"],
                input_reader=lambda: raw,
            )
            self.assertFalse(os.path.exists(config_path))
            result = run_config_command(
                ["import-freeform", "--config", config_path, "--stdin", "--yes", "--json"],
                input_reader=lambda: raw,
            )
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        self.assertEqual(preview["summary"], {"total": 2, "created": 2, "updated": 0})
        self.assertTrue(result["ready"])
        self.assertEqual([item["phone_source_label"] for item in cfg["accounts"]], ["A-SMS", "B-SMS"])
        sanitized = json.dumps(result, ensure_ascii=False)
        for secret in ("SECRET_A", "SECRET_B", "TOKEN_A", "TOKEN_B"):
            self.assertNotIn(secret, sanitized)

    def test_import_freeform_from_stdin_writes_config_and_sanitizes_output(self):
        raw = (
            "login@example.com----SECRET_PASSWORD---JBSWY3DPEHPK3PXP"
            "----+15550123456----https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN"
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            run_config_command(["init", "--config", config_path])

            result = run_config_command(
                [
                    "import-freeform",
                    "--config",
                    config_path,
                    "--label",
                    "ChatGPT",
                    "--stdin",
                    "--yes",
                    "--json",
                ],
                input_reader=lambda: raw,
            )

            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.assertTrue(result["ready"])
            self.assertEqual(len(cfg["fixed_sources"]), 1)
            self.assertEqual(cfg["fixed_sources"][0]["label"], "ChatGPT-SMS")
            self.assertEqual(cfg["fixed_sources"][0]["phone"], "+15550123456")
            self.assertEqual(cfg["fixed_sources"][0]["url"], "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN")
            self.assertEqual(len(cfg["accounts"]), 1)
            self.assertEqual(cfg["accounts"][0]["label"], "ChatGPT")
            self.assertEqual(cfg["accounts"][0]["login_email"], "login@example.com")
            self.assertEqual(cfg["accounts"][0]["password"], "SECRET_PASSWORD")
            self.assertEqual(cfg["accounts"][0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertEqual(cfg["accounts"][0]["phone"], "+15550123456")
            sanitized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SECRET_PASSWORD", sanitized)
            self.assertNotIn("JBSWY3DPEHPK3PXP", sanitized)
            self.assertNotIn("SECRET_TOKEN", sanitized)

    def test_import_freeform_interactive_fills_missing_fields(self):
        raw = "login@example.com----SECRET_PASSWORD---JBSWY3DPEHPK3PXP"
        answers = iter(
            [
                "15550123456",
                "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            run_config_command(["init", "--config", config_path])

            result = run_config_command(
                [
                    "import-freeform",
                    "--config",
                    config_path,
                    "--label",
                    "ChatGPT",
                    "--stdin",
                    "--interactive",
                    "--yes",
                    "--json",
                ],
                input_reader=lambda: raw,
                prompt_reader=lambda prompt, secret=False: next(answers),
            )

            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.assertTrue(result["ready"])
            self.assertEqual(cfg["fixed_sources"][0]["phone"], "15550123456")
            self.assertEqual(cfg["fixed_sources"][0]["url"], "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN")
            sanitized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SECRET_TOKEN", sanitized)

    def _focus_config(self):
        return {
            "base_url": "https://example.invalid",
            "key": "OLD_LUDAN_KEY",
            "request_timeout": 1,
            "fixed_sources": [
                {"label": "first", "phone": "+15550123456", "url": "https://sms/first?OLD_TOKEN"},
                {"label": "duplicate", "phone": "+15550123456", "url": "https://sms/duplicate?OLD_TOKEN"},
                {"label": "other", "phone": "+15550999999", "url": "https://sms/other?OLD_TOKEN"},
            ],
            "kkdos_sources": [{"label": "kkdos", "cdk": "OLD_KKDOS"}],
            "msgnest_sources": [{"label": "msgnest", "cdk": "OLD_MSGNEST"}],
            "email_sources": [
                {
                    "label": "old-mail",
                    "provider": "icloud",
                    "email": "old@icloud.com",
                    "base_url": "https://email.nloop.cc",
                }
            ],
            "accounts": [
                {
                    "label": "target",
                    "login_email": "82target@example.com",
                    "password": "OLD_PASSWORD",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "phone": "+15550123456",
                    "email": "old@icloud.com",
                    "note": "keep-me",
                },
                {"label": "other", "login_email": "other@example.com"},
            ],
            "disabled": {"fixed:other": {"reason": "old", "at": "yesterday"}},
        }

    @staticmethod
    def _read_bytes(path):
        with open(path, "rb") as f:
            return f.read()

    def test_upsert_songniqu_prompt_is_idempotent_and_secret_free(self):
        mailbox = "user@example.com=SECRET_KEY"
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            for _ in range(2):
                result = run_config_command(
                    [
                        "upsert-email",
                        "--config",
                        config_path,
                        "--label",
                        "Songniqu",
                        "--provider",
                        "songniqu",
                        "--mailbox-prompt",
                    ],
                    prompt_reader=lambda prompt, secret=False: mailbox,
                )
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        self.assertEqual(len(cfg["email_sources"]), 1)
        self.assertEqual(cfg["email_sources"][0]["email"], "user@example.com")
        self.assertEqual(cfg["email_sources"][0]["mailbox"], mailbox)
        self.assertNotIn("SECRET_KEY", json.dumps(result, ensure_ascii=False))

    def test_focus_account_dry_run_does_not_prompt_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self._focus_config(), f)
            before = self._read_bytes(config_path)

            result = run_config_command(
                ["focus-account", "--config", config_path, "--login-prefix", "82", "--json"],
                prompt_reader=lambda *args, **kwargs: self.fail("dry-run must not prompt"),
            )
            after = self._read_bytes(config_path)

        self.assertEqual(before, after)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["target_email"], "82***@example.com")
        self.assertEqual(result["phone_source_kind"], "fixed")
        self.assertEqual(result["counts"]["accounts"], {"before": 2, "after": 1, "removed": 1})
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in ("OLD_LUDAN_KEY", "OLD_PASSWORD", "OLD_TOKEN", "OLD_KKDOS", "OLD_MSGNEST"):
            self.assertNotIn(secret, serialized)

    def test_focus_account_requires_exactly_one_match_without_writing(self):
        for accounts in (
            [{"login_email": "other@example.com"}],
            [{"login_email": "82a@example.com"}, {"login_email": "82b@example.com"}],
        ):
            with self.subTest(count=len(accounts)), tempfile.TemporaryDirectory() as tmp:
                config_path = os.path.join(tmp, "config.json")
                cfg = self._focus_config()
                cfg["accounts"] = accounts
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f)
                before = self._read_bytes(config_path)
                with self.assertRaises(ConfigCommandError):
                    run_config_command(
                        ["focus-account", "--config", config_path, "--login-prefix", "82"]
                    )
                self.assertEqual(before, self._read_bytes(config_path))

    def test_focus_account_failed_precheck_keeps_original_config_and_hides_secret(self):
        mailbox = "new@example.com=SECRET_KEY"
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self._focus_config(), f)
            before = self._read_bytes(config_path)

            result = run_config_command(
                [
                    "focus-account",
                    "--config",
                    config_path,
                    "--login-prefix",
                    "82",
                    "--mailbox-prompt",
                    "--yes",
                ],
                prompt_reader=lambda prompt, secret=False: mailbox,
                session_factory=lambda: EmailReadySession(
                    FakeJsonResponse({"ok": False, "message": mailbox})
                ),
            )
            after = self._read_bytes(config_path)

        self.assertEqual(before, after)
        self.assertEqual(result["status"], "api_not_ready")
        self.assertNotIn("SECRET_KEY", json.dumps(result, ensure_ascii=False))

    def test_focus_account_success_is_atomic_and_keeps_first_runtime_phone_source(self):
        mailbox = "new@example.com=SECRET_KEY"
        session = EmailReadySession(FakeJsonResponse({"ok": True, "mails": []}))
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self._focus_config(), f)

            result = run_config_command(
                [
                    "focus-account",
                    "--config",
                    config_path,
                    "--login-prefix",
                    "82",
                    "--mailbox-prompt",
                    "--yes",
                ],
                prompt_reader=lambda prompt, secret=False: mailbox,
                session_factory=lambda: session,
            )
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "focused")
        self.assertEqual(cfg["key"], "")
        self.assertEqual(cfg["disabled"], {})
        self.assertEqual(len(cfg["accounts"]), 1)
        self.assertEqual(cfg["accounts"][0]["login_email"], "82target@example.com")
        self.assertEqual(cfg["accounts"][0]["password"], "OLD_PASSWORD")
        self.assertEqual(cfg["accounts"][0]["note"], "keep-me")
        self.assertEqual(cfg["accounts"][0]["email"], "new@example.com")
        self.assertEqual(cfg["accounts"][0]["phone_source_label"], "first")
        self.assertEqual([item["label"] for item in cfg["fixed_sources"]], ["first"])
        self.assertEqual(cfg["kkdos_sources"], [])
        self.assertEqual(cfg["msgnest_sources"], [])
        self.assertEqual(len(cfg["email_sources"]), 1)
        self.assertEqual(cfg["email_sources"][0]["mailbox"], mailbox)
        self.assertNotIn("SECRET_KEY", json.dumps(result, ensure_ascii=False))


class ReadyCheckTest(unittest.TestCase):
    """验证 ready-check 只判断预备接码状态，不要求已经收到验证码。"""

    def test_ludan_ready_when_verify_already_has_usable_phone(self):
        cfg = {"base_url": "https://example.invalid", "key": "dummy", "request_timeout": 1}
        session = ActionSession(
            {
                "verify": [
                    {
                        "code": 0,
                        "data": {"phone": "15550123456", "remaining_seconds": 120},
                    }
                ]
            }
        )

        result = ready_check_ludan(cfg, session)

        self.assertTrue(result["ready"])
        self.assertEqual(result["label"], "LuDan")
        self.assertEqual(session.actions, ["verify"])

    def test_ludan_ready_when_get_number_returns_phone_after_verify(self):
        cfg = {"base_url": "https://example.invalid", "key": "dummy", "request_timeout": 1}
        session = ActionSession(
            {
                "verify": [{"code": 0, "data": {"has_number": False}}],
                "get_number": [{"code": 0, "data": {"phone": "15550987654", "expires_in": 300}}],
            }
        )

        result = ready_check_ludan(cfg, session)

        self.assertTrue(result["ready"])
        self.assertEqual(session.actions, ["verify", "get_number"])

    def test_ludan_not_ready_when_cdk_fails(self):
        cfg = {"base_url": "https://example.invalid", "key": "dummy", "request_timeout": 1}
        session = ActionSession({"verify": [{"code": 401, "msg": "bad secret"}]})

        result = ready_check_ludan(cfg, session)

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "auth_failed")
        self.assertNotIn("bad secret", json.dumps(result, ensure_ascii=False))

    def test_fixed_url_ready_for_reachable_waiting_or_code_states(self):
        cfg = {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/sms"}
        cases = [
            FakeTextResponse("暂无短信"),
            FakeTextResponse("hello page without code"),
            FakeTextResponse("Your verification code is 123456"),
        ]

        for response in cases:
            result = ready_check_fixed_source(cfg, FixedReadySession(response), request_timeout=1)
            self.assertTrue(result["ready"], result)
            self.assertEqual(result["label"], "YunTL")
            self.assertEqual(result["kind"], "fixed")

    def test_fixed_url_not_ready_for_http_error(self):
        cfg = {"label": "YunTL", "phone": "15550123456", "url": "https://example.invalid/sms"}
        result = ready_check_fixed_source(
            cfg,
            FixedReadySession(FakeTextResponse("server error", status_code=500)),
            request_timeout=1,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "http_error")

    def test_email_ready_when_query_api_ok_even_without_mail(self):
        cfg = {
            "label": "iCloud",
            "provider": "icloud",
            "base_url": "https://email.nloop.cc",
            "email": "user@icloud.com",
        }
        result = ready_check_email_source(
            cfg,
            EmailReadySession(FakeJsonResponse({"ok": True, "mails": []})),
            request_timeout=1,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")

    def test_email_not_ready_for_bad_json_or_ok_false(self):
        cfg = {
            "label": "iCloud",
            "provider": "icloud",
            "base_url": "https://email.nloop.cc",
            "email": "user@icloud.com",
        }
        bad_json = ready_check_email_source(
            cfg,
            EmailReadySession(FakeTextResponse("not json")),
            request_timeout=1,
        )
        ok_false = ready_check_email_source(
            cfg,
            EmailReadySession(FakeJsonResponse({"ok": False, "error": "secret detail"})),
            request_timeout=1,
        )

        self.assertFalse(bad_json["ready"])
        self.assertEqual(bad_json["status"], "bad_response")
        self.assertFalse(ok_false["ready"])
        self.assertEqual(ok_false["status"], "api_not_ready")
        self.assertNotIn("secret detail", json.dumps(ok_false, ensure_ascii=False))

    def test_songniqu_ready_uses_receive_contract_without_leaking(self):
        mailbox = "user@example.com=SECRET_KEY"
        cfg = {
            "label": "Songniqu",
            "provider": "songniqu",
            "base_url": "https://mail.songniqu.cfd",
            "email": "user@example.com",
            "mailbox": mailbox,
        }
        session = EmailReadySession(FakeJsonResponse({"ok": True, "mails": []}))

        result = ready_check_email_source(cfg, session, request_timeout=2)

        self.assertTrue(result["ready"])
        self.assertEqual(
            session.calls[0],
            (
                "https://mail.songniqu.cfd/api/receive",
                {"mailbox": mailbox, "turnstile_token": ""},
                2,
            ),
        )
        self.assertNotIn("SECRET_KEY", json.dumps(result, ensure_ascii=False))

    def test_songniqu_ready_failure_codes_are_sanitized(self):
        mailbox = "user@example.com=SECRET_KEY"
        cfg = {
            "label": "Songniqu",
            "provider": "songniqu",
            "base_url": "https://mail.songniqu.cfd",
            "email": "user@example.com",
            "mailbox": mailbox,
        }
        for payload, expected in [
            ({"ok": False, "code": "turnstile_failed", "message": mailbox}, "turnstile_required"),
            ({"ok": False, "code": "mailbox_bound", "credential": mailbox}, "mailbox_bound"),
            ({"ok": False, "message": mailbox}, "api_not_ready"),
        ]:
            result = ready_check_email_source(
                cfg, EmailReadySession(FakeJsonResponse(payload)), request_timeout=1
            )
            self.assertFalse(result["ready"])
            self.assertEqual(result["status"], expected)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(mailbox, serialized)
            self.assertNotIn("SECRET_KEY", serialized)

        forbidden = ready_check_email_source(
            cfg,
            EmailReadySession(
                FakeJsonResponse({"ok": False, "message": mailbox}, status_code=403)
            ),
            request_timeout=1,
        )
        self.assertEqual(forbidden["status"], "api_not_ready")
        self.assertNotIn("SECRET_KEY", json.dumps(forbidden, ensure_ascii=False))


class KkdosSourceTest(unittest.TestCase):
    """验证 kkdos 私有 HTTP/SSE 流程可用，且不依赖浏览器或视觉识别。"""

    def _verify_payload(self, **extra):
        data = {
            "sessionId": "sess-123",
            "phone": "+15550123456",
            "state": "assigned",
            "type": "bindable",
            "locked": False,
            "attempt": 1,
            "maxAttempts": 3,
            "trialSwitchCount": 0,
            "maxTrialSwitches": 3,
            "history": [{"content": "OpenAI verification code 246810"}],
            "expiresAt": "2026-07-30T10:59:09.000Z",
            "effectiveExpiresAt": "2026-07-30T10:59:09.000Z",
        }
        data.update(extra)
        return {"success": True, "data": data}

    def test_verify_sets_session_phone_and_history_code(self):
        session = KkdosFakeSession(verify=self._verify_payload())
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)

        source.verify()

        self.assertEqual(source.session_id, "sess-123")
        self.assertEqual(source.phone, "+15550123456")
        self.assertEqual(source.state, "assigned")
        self.assertEqual(source.last_code, "246810")
        self.assertEqual(source.history[0][1], "246810")
        self.assertEqual(session.posts[0][0], "https://sms.kkdos.store/api/cdk/verify")

    def test_verify_history_code_field(self):
        """实测 kkdos verify 的 history：验证码在 code 字段，无 content 字段。

        回归：修复前 _record_history_item 只读 content/data/message，kkdos
        history 实际把码放在 code 字段，导致历史码一条都回填不了。
        """
        payload = self._verify_payload(
            history=[{"sessionId": "s1", "state": "succeeded", "code": "651663"}]
        )
        session = KkdosFakeSession(verify=payload)
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)

        source.verify()

        self.assertEqual(source.last_code, "651663")
        self.assertEqual(source.history[0][1], "651663")

    def test_poll_starts_session_and_reads_sse_code(self):
        session = KkdosFakeSession(
            verify=self._verify_payload(),
            start={"success": True, "data": {"attempt": 1, "maxAttempts": 3}},
            sse=[{"type": "connected"}, {"type": "code", "data": "Your OpenAI code is 654321"}],
        )
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)
        source.verify()
        source.start_waiting_for_code()

        self.assertEqual(source.poll(), "654321")

        self.assertTrue(session.posts[1][0].endswith("/api/session/sess-123/start"))
        self.assertTrue(session.gets[0][0].endswith("/api/sse/sess-123"))
        self.assertEqual(source.last_code, "654321")
        self.assertEqual(source.status, "收到新验证码")

    def test_poll_sse_code_field(self):
        """kkdos SSE code 事件：验证码可能在 code 字段（与 history 同源）。"""
        session = KkdosFakeSession(
            verify=self._verify_payload(history=[]),
            start={"success": True, "data": {"attempt": 1, "maxAttempts": 3}},
            sse=[{"type": "connected"}, {"type": "code", "code": "654321"}],
        )
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)
        source.verify()
        source.start_waiting_for_code()

        self.assertEqual(source.poll(), "654321")
        self.assertEqual(source.last_code, "654321")
        self.assertEqual(source.status, "收到新验证码")

    def test_poll_idle_does_not_return_code(self):
        session = KkdosFakeSession(
            verify=self._verify_payload(),
            start={"success": True, "data": {"attempt": 1, "maxAttempts": 3}},
            sse=[{"type": "idle", "remainingSeconds": 0}],
        )
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)
        source.verify()
        source.start_waiting_for_code()

        self.assertIsNone(source.poll())
        self.assertEqual(source.status, "等待触发")
        self.assertIn("未收到验证码", source.note)

    def test_poll_swallows_verify_error_without_raising(self):
        # session_id 为空时 poll() 会调 verify()；verify 失败（success=false）
        # 抛 KkdosApiError，必须被 poll 内部捕获，不能冒泡拖垮监控主循环。
        session = KkdosFakeSession(verify={"success": False, "error": "CDK 已失效"})
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, session, request_timeout=1)
        source.start_waiting_for_code()

        result = source.poll()

        self.assertIsNone(result)
        self.assertEqual(source.status, "校验失败")
        self.assertEqual(source.note, "kkdos 请求被拒绝")
        # 脱敏：CDK 明文不得出现在状态信息里
        self.assertNotIn("SECRET_CDK", source.note)

    def test_change_number_handles_success_cooldown_and_locked(self):
        success_session = KkdosFakeSession(
            verify=self._verify_payload(),
            switch={
                "success": True,
                "data": {
                    "phone": "+15550999999",
                    "attempt": 2,
                    "maxAttempts": 3,
                    "trialSwitchCount": 1,
                    "maxTrialSwitches": 3,
                },
            },
        )
        source = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, success_session, request_timeout=1)
        source.verify()
        source.change_number()
        self.assertEqual(source.phone, "+15550999999")
        self.assertEqual(source.last_code, "")
        self.assertEqual(source.trial_switch_count, 1)

        cooldown_session = KkdosFakeSession(
            verify=self._verify_payload(),
            switch={"success": False, "error": "too soon", "data": {"remainingSeconds": 17}},
        )
        cooldown = KkdosSource({"label": "kkdos", "cdk": "SECRET_CDK"}, cooldown_session, request_timeout=1)
        cooldown.verify()
        cooldown.change_number()
        self.assertIn("17", cooldown.note)

        locked = KkdosSource(
            {"label": "kkdos", "cdk": "SECRET_CDK"},
            KkdosFakeSession(verify=self._verify_payload(locked=True)),
            request_timeout=1,
        )
        locked.verify()
        locked.change_number()
        self.assertIn("锁定", locked.note)

    def test_ready_check_kkdos_ready_when_verify_returns_phone(self):
        session = KkdosFakeSession(verify=self._verify_payload())
        result = ready_check_kkdos_source({"label": "kkdos", "cdk": "SECRET_CDK"}, session, 1)

        self.assertTrue(result["ready"])
        self.assertEqual(result["kind"], "kkdos")
        self.assertNotIn("SECRET_CDK", json.dumps(result, ensure_ascii=False))


class LuDanSourceTest(unittest.TestCase):
    """验证 LuDan 动态号使用官网同款 action 流程。"""

    def test_refresh_number_reuses_verify_payload_without_status_action(self):
        session = FakeSession(
            [
                {
                    "code": 0,
                    "data": {
                        "phone": "15550123456",
                        "card_status": "used",
                        "sms_count": 2,
                        "switch_count": 1,
                        "remaining_seconds": 120,
                    },
                }
            ]
        )
        source = LuDanSource({"base_url": "https://example.invalid", "key": "dummy"}, session)

        source.verify()
        source.refresh_number()

        self.assertEqual(session.actions, ["verify"])
        self.assertEqual(source.phone, "15550123456")
        self.assertEqual(source.sms_count, 2)
        self.assertEqual(source.switch_count, 1)
        self.assertEqual(source.expires_in, 120)

    def test_refresh_number_calls_get_number_when_verify_has_no_phone(self):
        session = FakeSession(
            [
                {
                    "code": 0,
                    "data": {"has_number": False, "card_status": "unused"},
                },
                {
                    "code": 0,
                    "data": {"phone": "15550987654", "expires_in": 300},
                },
            ]
        )
        source = LuDanSource({"base_url": "https://example.invalid", "key": "dummy"}, session)

        source.verify()
        source.refresh_number()

        self.assertEqual(session.actions, ["verify", "get_number"])
        self.assertEqual(source.phone, "15550987654")
        self.assertEqual(source.expires_in, 300)

    def test_poll_accepts_code_field_fallback(self):
        session = FakeSession(
            [
                {
                    "code": 0,
                    "data": {"has_sms": True, "code": "123456", "content": "code 123456"},
                }
            ]
        )
        source = LuDanSource({"base_url": "https://example.invalid", "key": "dummy"}, session)

        self.assertEqual(source.poll(), "123456")
        self.assertEqual(source.last_code, "123456")


class ClipboardBehaviorTest(unittest.TestCase):
    """验证新验证码走自动剪贴板，号码仍由热键手动复制。"""

    def test_auto_copy_new_code_sets_monitor_note(self):
        copied = []
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor.note = ""

        def fake_copy(text):
            copied.append(text)
            return True

        ok = monitor.auto_copy_code("YunTL", "123456", fake_copy)

        self.assertTrue(ok)
        self.assertEqual(copied, ["123456"])
        self.assertEqual(monitor.note, "已自动复制 [YunTL] 的验证码")

    def test_multiple_new_codes_copy_first_and_list_rest(self):
        copied = []
        monitor = SmsMonitor.__new__(SmsMonitor)
        monitor.note = ""
        monitor.active_until_code = True

        def fake_copy(text):
            copied.append(text)
            return True

        monitor.auto_copy_codes([("LuDan", "111111"), ("YunTL", "222222")], fake_copy)

        # 剪贴板只放第一个，其余在 note 里完整列出，不被静默覆盖
        self.assertEqual(copied, ["111111"])
        self.assertIn("已自动复制 [LuDan] 的验证码", monitor.note)
        self.assertIn("[YunTL] 222222", monitor.note)

    def test_copy_to_clipboard_retries_windows_writer_before_fallback(self):
        attempts = []

        def flaky_writer(text):
            attempts.append(text)
            return len(attempts) == 2

        def unused_fallback(text):
            self.fail("Windows writer succeeded after retry; fallback should not run")

        self.assertTrue(
            monitor.copy_to_clipboard(
                "123456",
                attempts=2,
                retry_delay=0,
                windows_writer=flaky_writer,
                fallback_writer=unused_fallback,
            )
        )
        self.assertEqual(attempts, ["123456", "123456"])

    def test_copy_to_clipboard_uses_fallback_after_windows_writer_fails(self):
        windows_attempts = []
        fallback_calls = []

        def failing_windows_writer(text):
            windows_attempts.append(text)
            return False

        def fallback_writer(text):
            fallback_calls.append(text)
            return True

        self.assertTrue(
            monitor.copy_to_clipboard(
                "654321",
                attempts=2,
                retry_delay=0,
                windows_writer=failing_windows_writer,
                fallback_writer=fallback_writer,
            )
        )
        self.assertEqual(windows_attempts, ["654321", "654321"])
        self.assertEqual(fallback_calls, ["654321"])


class LuDanOptionalTest(unittest.TestCase):
    """LuDan 可选化：key 未配置或占位时跳过 LuDan，不阻塞其余来源。"""

    def _cfg(self, **extra):
        cfg = {
            "base_url": "https://example.invalid",
            "request_timeout": 1,
            "fixed_sources": [],
            "email_sources": [],
            "accounts": [],
        }
        cfg.update(extra)
        return cfg

    def test_validate_passes_when_key_empty(self):
        result = validate_config_result(self._cfg())
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "valid")

    def test_validate_passes_when_key_is_placeholder(self):
        result = validate_config_result(self._cfg(key="YOUR_CDK"))
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "valid")

    def test_ready_check_all_skips_ludan_when_key_empty(self):
        # key 为空时不应调用 LuDan；factory 若被调用即说明未跳过
        def factory():
            raise AssertionError("LuDan 不应在 key 为空时被检查")

        result = ready_check_all(self._cfg(), factory)
        self.assertTrue(result["ready"])
        self.assertEqual([i["label"] for i in result["items"]], [])

    def test_ready_check_single_reports_not_configured_when_key_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self._cfg(), f)
            result = run_config_command(["ready-check", "--config", config_path, "--json"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["items"][0]["status"], "not_configured")

    def test_sms_monitor_ludan_none_when_key_empty(self):
        cfg = monitor.default_config()
        m = SmsMonitor(cfg)
        self.assertIsNone(m.ludan)

    def test_sms_monitor_ludan_none_when_key_placeholder(self):
        cfg = monitor.default_config()
        cfg["key"] = "YOUR_CDK"
        m = SmsMonitor(cfg)
        self.assertIsNone(m.ludan)


class MsgNestResponse:
    """msg-nest 测试响应；支持自定义 status_code 模拟 401 等。"""

    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class MsgNestFakeSession:
    """记录 msg-nest GET/POST，按路径与入队顺序返回预设响应。"""

    def __init__(self):
        self.posts = []
        self.gets = []
        self._redeem = []
        self._alloc = []
        self._messages = []

    def queue_redeem(self, payload, status=200):
        self._redeem.append(MsgNestResponse(payload, status))

    def queue_allocation(self, payload, status=200):
        self._alloc.append(MsgNestResponse(payload, status))

    def queue_messages(self, payload, status=200):
        self._messages.append(MsgNestResponse(payload, status))

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if self._redeem:
            return self._redeem.pop(0)
        return MsgNestResponse({}, 200)

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/messages"):
            return self._messages.pop(0) if self._messages else MsgNestResponse({}, 200)
        return self._alloc.pop(0) if self._alloc else MsgNestResponse({}, 200)


class MsgNestSourceTest(unittest.TestCase):
    """msg-nest 来源：normalize 持久化、redeem 号码核对、poll 取码、401 自愈。"""

    def _entry(self, **overrides):
        entry = {
            "label": "sms",
            "cdk": "TEST-CDK-FAKE-0001",
            "base_url": "https://msg-nest.com",
            "alloc_id": "",
            "claim_token": "",
            "fingerprint": "",
            "phone": "5551234567",
        }
        entry.update(overrides)
        return entry

    def test_normalize_preserves_persisted_state(self):
        out = normalize_msgnest_sources(
            [self._entry(alloc_id="alloc_1", claim_token="tok", fingerprint="fp")]
        )
        self.assertEqual(out[0]["alloc_id"], "alloc_1")
        self.assertEqual(out[0]["claim_token"], "tok")
        self.assertEqual(out[0]["fingerprint"], "fp")

    def test_normalize_missing_cdk_raises(self):
        with self.assertRaises(ConfigCommandError):
            normalize_msgnest_sources([{"label": "x", "base_url": "https://msg-nest.com"}])

    def test_redeem_phone_match(self):
        session = MsgNestFakeSession()
        session.queue_redeem(
            {"data": {"allocId": "alloc_testfake0001", "claimToken": "tok1", "phone": "+15551234567", "expiresAt": "2026-07-27T15:00:00Z"}}
        )
        session.queue_allocation({"data": {"phone": "+15551234567", "expiresAt": "2026-07-27T15:00:00Z"}})
        ms = MsgNestSource(self._entry(), session, 3.0)
        ms.verify()
        self.assertEqual(ms.phone, "5551234567")
        self.assertEqual(ms.status, "已分配号码")
        self.assertEqual(ms.note, "已兑换")
        self.assertEqual(ms.claim_token, "tok1")

    def test_redeem_phone_mismatch_warning(self):
        session = MsgNestFakeSession()
        session.queue_redeem({"data": {"allocId": "alloc_x", "claimToken": "t", "phone": "+15559876543"}})
        session.queue_allocation({"data": {"phone": "+15559876543"}})
        ms = MsgNestSource(self._entry(), session, 3.0)
        ms.verify()
        self.assertIn("号码与预期不符", ms.note)

    def test_poll_extracts_code(self):
        session = MsgNestFakeSession()
        session.queue_messages({"data": {"messages": [{"content": "Your verification code is 123456"}]}})
        ms = MsgNestSource(self._entry(alloc_id="alloc_1", claim_token="tok", fingerprint="fp"), session, 3.0)
        code = ms.poll()
        self.assertEqual(code, "123456")
        self.assertEqual(ms.last_code, "123456")
        self.assertEqual(ms.status, "收到新验证码")

    def test_poll_extracts_code_from_code_field(self):
        """实测 msg-nest messages：验证码在 code 字段，无 content 字段。

        回归：修复前 poll 只读 content/body/text/message，msg-nest 实际把码放在
        code 字段，导致一条码都提取不到。
        """
        session = MsgNestFakeSession()
        session.queue_messages(
            {"messages": [{"id": "sms_x", "code": "859325", "receivedAt": "2026-08-04T00:56:00.000Z"}]}
        )
        ms = MsgNestSource(self._entry(alloc_id="alloc_1", claim_token="tok", fingerprint="fp"), session, 3.0)
        code = ms.poll()
        self.assertEqual(code, "859325")
        self.assertEqual(ms.last_code, "859325")
        self.assertEqual(ms.status, "收到新验证码")

    def test_poll_does_not_return_older_code_when_latest_seen(self):
        """messages 倒序，最新码已见过时不应返回更旧的历史码。

        回归：修复前遍历不会在 last_code 处停止，会把更旧的码当新码返回并
        复制，表现为"收到的不是最新验证码、反而是更旧的"。
        """
        session = MsgNestFakeSession()
        session.queue_messages({"messages": [{"code": "859325"}, {"code": "181921"}]})
        ms = MsgNestSource(self._entry(alloc_id="alloc_1", claim_token="tok", fingerprint="fp"), session, 3.0)
        ms.last_code = "859325"  # 最新码已见过

        self.assertIsNone(ms.poll())
        self.assertEqual(ms.last_code, "859325")
        self.assertEqual(ms.status, "收到短信")

    def test_poll_returns_newer_code_when_latest_unseen(self):
        """最新码未见过时返回最新码，跳过中间更旧的历史码。"""
        session = MsgNestFakeSession()
        session.queue_messages(
            {"messages": [{"code": "999999"}, {"code": "859325"}, {"code": "181921"}]}
        )
        ms = MsgNestSource(self._entry(alloc_id="alloc_1", claim_token="tok", fingerprint="fp"), session, 3.0)
        ms.last_code = "859325"  # 见过 859325，但 999999 是新的

        self.assertEqual(ms.poll(), "999999")
        self.assertEqual(ms.last_code, "999999")

    def test_poll_401_self_heal(self):
        session = MsgNestFakeSession()
        session.queue_messages({"message": "expired"}, status=401)
        session.queue_redeem({"data": {"allocId": "alloc_1", "claimToken": "fresh", "phone": "+15551234567"}})
        session.queue_messages({"data": {"messages": [{"content": "code: 987654"}]}})
        ms = MsgNestSource(self._entry(alloc_id="alloc_1", claim_token="stale", fingerprint="fp"), session, 3.0)
        code = ms.poll()
        self.assertEqual(code, "987654")
        self.assertEqual(ms.claim_token, "fresh")
        self.assertEqual(len(session.posts), 1)

    def test_ready_check_ready(self):
        session = MsgNestFakeSession()
        session.queue_redeem({"data": {"allocId": "a", "claimToken": "t", "phone": "+15551234567"}})
        session.queue_allocation({"data": {"phone": "+15551234567"}})
        result = ready_check_msgnest_source(self._entry(), session, 3.0)
        self.assertTrue(result["ready"])
        self.assertEqual(result["kind"], "msgnest")

    def test_ready_check_not_ready_on_auth_error(self):
        session = MsgNestFakeSession()
        session.queue_redeem({"message": "invalid cdk"}, status=403)
        result = ready_check_msgnest_source(self._entry(), session, 3.0)
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "api_not_ready")

    def test_phone_normalization(self):
        ms = MsgNestSource(self._entry(), MsgNestFakeSession(), 3.0)
        self.assertEqual(ms._normalize_phone("+15551234567"), "5551234567")
        self.assertEqual(ms._normalize_phone("15551234567"), "5551234567")
        self.assertEqual(ms._normalize_phone("5551234567"), "5551234567")


if __name__ == "__main__":
    unittest.main()
