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
    LuDanSource,
    SmsMonitor,
    generate_totp,
    normalize_accounts,
    normalize_email_sources,
    parse_fixed_sms_response,
    parse_freeform_account_text,
    ready_check_email_source,
    ready_check_fixed_source,
    ready_check_ludan,
    run_cli,
    run_config_command,
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

    def post(self, url, json=None, timeout=None):
        if self.exc:
            raise self.exc
        return self.response


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

    def test_copy_fields_only_exposes_login_email_and_valid_totp(self):
        account = AccountSource(
            {
                "label": "ChatGPT",
                "login_email": "user@example.com",
                "password": "pw",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "phone": "15550123456",
            }
        )

        with patch("monitor.generate_totp", return_value="123456"):
            self.assertEqual(
                account.copy_fields(),
                [
                    ("1", "登录邮箱", "user@example.com"),
                    ("2", "2FA 动态码", "123456"),
                ],
            )

    def test_copy_fields_include_latest_linked_verification_codes(self):
        account = AccountSource({"label": "ChatGPT", "login_email": "user@example.com"})
        phone_source = FakePollable("YunTL")
        email_source = FakePollable("iCloudMail")
        phone_source.last_code = "111111"
        email_source.last_code = "222222"
        account.linked_phone_source = phone_source
        account.linked_email_source = email_source

        self.assertEqual(
            account.copy_fields(),
            [
                ("1", "登录邮箱", "user@example.com"),
                ("2", "手机验证码", "111111"),
                ("3", "邮箱验证码", "222222"),
            ],
        )

    def test_copy_fields_skip_empty_linked_verification_codes(self):
        account = AccountSource({"label": "ChatGPT", "login_email": "user@example.com"})
        account.linked_phone_source = FakePollable("YunTL")
        account.linked_email_source = FakePollable("iCloudMail")
        account.linked_phone_source.last_code = ""
        account.linked_email_source.last_code = ""

        self.assertEqual(account.copy_fields(), [("1", "登录邮箱", "user@example.com")])

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
            monitor.copy_source_number(2)

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
            monitor.copy_source_number(2)

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
            self.assertEqual(len(cfg["email_sources"]), 1)
            self.assertEqual(cfg["email_sources"][0]["email"], "user@icloud.com")
            self.assertEqual(len(cfg["accounts"]), 1)
            self.assertEqual(cfg["accounts"][0]["phone"], "15550999999")
            self.assertEqual(cfg["accounts"][0]["email"], "user@icloud.com")
            self.assertEqual(cfg["accounts"][0]["password"], "SECRET_PASSWORD")
            self.assertEqual(cfg["accounts"][0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            sanitized = json.dumps(account_result, ensure_ascii=False)
            self.assertNotIn("SECRET_PASSWORD", sanitized)
            self.assertNotIn("JBSWY3DPEHPK3PXP", sanitized)

    def test_cli_validate_and_ready_check_json_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            env = {
                "SMS_MONITOR_KEY": "REAL_SECRET_KEY",
                "FIXED_URL": "https://example.invalid/sms?token=SECRET_TOKEN",
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
            output = validate_stdout.getvalue() + ready_stdout.getvalue()
            for secret in ["REAL_SECRET_KEY", "SECRET_TOKEN", "SECRET_PASSWORD", "JBSWY3DPEHPK3PXP"]:
                self.assertNotIn(secret, output)

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
        self.assertEqual(parsed["phone"], "15550123456")
        self.assertEqual(parsed["sms_url"], "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN")
        preview = json.dumps(parsed["preview"], ensure_ascii=False)
        self.assertNotIn("SECRET_PASSWORD", preview)
        self.assertNotIn("JBSWY3DPEHPK3PXP", preview)
        self.assertNotIn("SECRET_TOKEN", preview)

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
            self.assertEqual(cfg["fixed_sources"][0]["phone"], "15550123456")
            self.assertEqual(cfg["fixed_sources"][0]["url"], "https://sms.example/api/orders/abc/sms-url?token=SECRET_TOKEN")
            self.assertEqual(len(cfg["accounts"]), 1)
            self.assertEqual(cfg["accounts"][0]["label"], "ChatGPT")
            self.assertEqual(cfg["accounts"][0]["login_email"], "login@example.com")
            self.assertEqual(cfg["accounts"][0]["password"], "SECRET_PASSWORD")
            self.assertEqual(cfg["accounts"][0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertEqual(cfg["accounts"][0]["phone"], "15550123456")
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


if __name__ == "__main__":
    unittest.main()
