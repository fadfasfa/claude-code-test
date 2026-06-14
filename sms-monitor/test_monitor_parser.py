#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sms-monitor 解析规则测试。

本文件只覆盖固定文本接码链接的本地解析逻辑，避免真实 HTTP 响应里的到期时间
被误判成验证码。运行方式：`python sms-monitor/test_monitor_parser.py`。
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from monitor import (
    AccountSource,
    LuDanSource,
    SmsMonitor,
    generate_totp,
    normalize_accounts,
    normalize_email_sources,
    parse_fixed_sms_response,
    split_us_phone,
    totp_remaining,
)


class FakeJsonResponse:
    """测试 LuDan API 解析用的最小响应对象，避免访问真实服务。"""

    def __init__(self, payload):
        self.status_code = 200
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    """按调用顺序返回预设 payload，并记录 action。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.actions = []

    def get(self, url, params, timeout):
        self.actions.append(params["action"])
        return FakeJsonResponse(self.payloads.pop(0))


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
        result = split_us_phone("15550123456")

        self.assertEqual(result.country_code, "+1")
        self.assertEqual(result.local_number, "5550123456")

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


class NormalizeEmailSourcesTest(unittest.TestCase):
    """验证 email_sources 配置校验：缺 email 退出、provider 缺省与限制。"""

    def test_missing_email_exits(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
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
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                normalize_email_sources([{"email": "a@outlook.com", "provider": "outlook"}])


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
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
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

    def test_copy_fields_skip_empty_and_phone_uses_local_number(self):
        account = AccountSource(
            {
                "label": "ChatGPT",
                "login_email": "user@example.com",
                "password": "pw",
                "phone": "15550123456",
            }
        )

        self.assertEqual(
            account.copy_fields(),
            [
                ("1", "登录邮箱", "user@example.com"),
                ("2", "密码", "pw"),
                ("3", "关联电话", "5550123456"),
            ],
        )

    def test_poll_never_returns_code(self):
        account = AccountSource({"label": "iCloud", "login_email": "a@icloud.com"})

        self.assertIsNone(account.poll())
        self.assertEqual(account.copy_number, "a@icloud.com")

    def test_monitor_appends_accounts_after_code_sources(self):
        monitor = SmsMonitor(
            {
                "base_url": "https://example.invalid",
                "key": "dummy",
                "fixed_sources": [],
                "email_sources": [],
                "accounts": [{"label": "A", "login_email": "a@example.com"}],
            }
        )

        self.assertEqual([source.label for source in monitor.sources], ["LuDan", "A"])
        self.assertTrue(getattr(monitor.sources[-1], "is_account"))

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
            ["LuDan", "eSIM88", "ChatGPT", "iCloudAccount"],
        )
        self.assertEqual(
            [source.label for source in monitor.pollables],
            ["LuDan", "YunTL", "eSIM88", "ka001", "iCloudMail"],
        )
        self.assertEqual(monitor.accounts[0].linked_phone_source.label, "YunTL")
        self.assertEqual(monitor.accounts[1].linked_phone_source.label, "ka001")
        self.assertEqual(monitor.accounts[1].linked_email_source.label, "iCloudMail")


class RefreshModeTest(unittest.TestCase):
    """验证默认低频刷新、复制后高频等码，拿到验证码即停高频。"""

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
            monitor.copy_source_number(2)

        self.assertTrue(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)
        self.assertIn("已复制", monitor.note)

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
            monitor.copy_source_number(2)

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
        monitor.active_waiting_for_code = True
        monitor.active_until = 999

        def fake_copy(text):
            copied.append(text)
            return True

        monitor.auto_copy_codes([("LuDan", "111111")], fake_copy)

        self.assertEqual(copied, ["111111"])
        self.assertFalse(monitor.active_waiting_for_code)
        self.assertEqual(monitor.active_until, 0)


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

        def fake_copy(text):
            copied.append(text)
            return True

        monitor.auto_copy_codes([("LuDan", "111111"), ("YunTL", "222222")], fake_copy)

        # 剪贴板只放第一个，其余在 note 里完整列出，不被静默覆盖
        self.assertEqual(copied, ["111111"])
        self.assertIn("已自动复制 [LuDan] 的验证码", monitor.note)
        self.assertIn("[YunTL] 222222", monitor.note)


if __name__ == "__main__":
    unittest.main()
