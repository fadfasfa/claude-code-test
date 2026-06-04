#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LuDan SMS 接码验证码实时监控脚本。

职责：读取同目录 config.json 中的 CDK，轮询 LuDan SMS 开放 API，实时展示号码与最新验证码。
调用方：用户双击 run.bat 或命令行 `python monitor.py` 运行。
关键依赖：requests（HTTP 请求）；Windows 自带 clip 命令（剪贴板）；标准库 msvcrt（热键，可选）。
"""

import json
import os
import subprocess
import sys
import time
from collections import deque
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
    return cfg


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


class SmsMonitor:
    """封装一次监控会话的状态与 API 调用。"""

    def __init__(self, cfg):
        self.base_url = cfg["base_url"]
        self.key = cfg["key"]
        self.poll_interval = max(2, int(cfg.get("poll_interval", 5)))
        self.auto_change = bool(cfg.get("auto_change_on_expire", True))
        self.session = requests.Session()

        self.phone = ""
        self.last_code = ""
        self.history = deque(maxlen=8)  # 最近收到的验证码（带时间戳）
        self.card_status = "-"
        self.sms_count = 0
        self.switch_count = 0
        self.expires_in = None  # 号码剩余有效秒数（来自 API）
        self.note = ""  # 面板底部的一行临时提示
        self.clipboard_ok = False

    # ---------- API ----------
    def call(self, action):
        """调用开放 API，返回解析后的 dict；网络/限频错误内部重试。"""
        params = {"action": action, "key": self.key}
        for attempt in range(3):
            try:
                resp = self.session.get(self.base_url, params=params, timeout=10)
                if resp.status_code == 429:
                    self.note = "请求过于频繁，稍等重试…"
                    time.sleep(3)
                    continue
                data = resp.json()
                return data
            except requests.RequestException as e:
                self.note = f"网络异常重试中（{e.__class__.__name__}）"
                time.sleep(2)
            except ValueError:
                self.note = "接口返回非 JSON，稍后重试"
                time.sleep(2)
        return {"code": -1, "msg": "本地请求失败"}

    # ---------- 业务动作 ----------
    def verify(self):
        data = self.call("verify")
        if data.get("code") != 0:
            print(f"CDK 校验失败：{data.get('msg', '未知错误')}（code={data.get('code')}）")
            sys.exit(1)

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

    def poll_code(self):
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
                ts = datetime.now().strftime("%H:%M:%S")
                self.history.appendleft((ts, code, content))
                self.note = "★ 收到新验证码！"
        elif d.get("expired"):
            if self.auto_change:
                self.note = "号码过期，自动换号中…"
                self.change_number()
            else:
                self.note = "号码已过期，按 n 换号"

    # ---------- 辅助 ----------
    def _set_phone(self, phone):
        if phone and phone != self.phone:
            self.phone = phone
            self.clipboard_ok = copy_to_clipboard(phone)

    def _fmt_expire(self):
        if self.expires_in is None:
            return "-"
        try:
            secs = int(self.expires_in)
        except (TypeError, ValueError):
            return "-"
        if secs <= 0:
            return "已过期"
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def render(self):
        clear_screen()
        line = "=" * 46
        print(line)
        print("            LuDan SMS 验证码监控")
        print(line)
        print()
        print(f"  当前号码：  {self.phone or '(获取中)'}")
        clip_tip = "（已自动复制到剪贴板，可直接 Ctrl+V）" if self.clipboard_ok else "（剪贴板复制失败，请手动框选上面整行）"
        print(f"  {clip_tip}")
        print()
        print(f"  最新验证码：  {self.last_code or '等待中…'}")
        print()
        print("  最近收到：")
        if self.history:
            for ts, code, content in self.history:
                snippet = (content or "").replace("\n", " ")[:40]
                print(f"    [{ts}] {code}   {snippet}")
        else:
            print("    （暂无）")
        print()
        print(f"  状态：{self.card_status} | 短信:{self.sms_count} 次 | "
              f"换号:{self.switch_count} 次 | 有效期:{self._fmt_expire()}")
        print(f"  轮询中…（按 n 换号 / r 重新复制 / q 退出）" if msvcrt
              else "  轮询中…（Ctrl+C 退出）")
        if self.note:
            print(f"  >> {self.note}")
        print(line)

    def handle_keys(self):
        """非阻塞读取热键；返回 False 表示请求退出。"""
        if not msvcrt:
            return True
        while msvcrt.kbhit():
            ch = msvcrt.getwch().lower()
            if ch == "q":
                return False
            if ch == "n":
                self.note = "手动换号中…"
                self.render()
                self.change_number()
            elif ch == "r":
                self.clipboard_ok = copy_to_clipboard(self.phone)
                self.note = "已重新复制号码" if self.clipboard_ok else "复制失败"
        return True

    def run(self):
        print("正在校验 CDK…")
        self.verify()
        print("正在获取号码…")
        self.refresh_number()
        try:
            while True:
                self.poll_code()
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
