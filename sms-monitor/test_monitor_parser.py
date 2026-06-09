#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sms-monitor 解析规则测试。

本文件只覆盖固定文本接码链接的本地解析逻辑，避免真实 HTTP 响应里的到期时间
被误判成验证码。运行方式：`python sms-monitor/test_monitor_parser.py`。
"""

import unittest

from monitor import SmsMonitor, parse_fixed_sms_response, split_us_phone


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
