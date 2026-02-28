import tkinter as tk
import threading
import time
import ctypes
import json
import psutil
import requests
import urllib3
import base64
import pandas as pd
import win32gui
import os
import logging
from io import BytesIO
from PIL import Image, ImageTk
from datetime import datetime
from hero_sync import BASE_DIR, ASSET_DIR

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

os.makedirs(ASSET_DIR, exist_ok=True)

try:
    from hextech_query import get_latest_csv, display_hero_hextech, main_query, set_last_hero
    from hero_sync import load_champion_core_data, get_advanced_session
    from hextech_scraper import main_scraper
except ImportError:
    print("❌ 缺少核心依赖模块，请确保文件结构完整。")
    sys.exit(1)

class HextechUI:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception: pass

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.threads = []

        self.session = get_advanced_session()
        self.core_data = load_champion_core_data()

        self.check_and_sync_data()
        self.df = self.load_data()

        self.port, self.token = None, None
        self.current_hero_ids = set()
        self.image_cache = {}

        self.last_click_time = 0
        self.img_write_lock = threading.Lock()
        self.downloading_imgs = set()
        self._df_lock = threading.Lock()  # 保护 self.df 的多线程读写

        # 读取本地开关配置
        self.settings_file = os.path.join(BASE_DIR, "config", "user_settings.json")
        self.web_enabled = True
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r") as f:
                    self.web_enabled = json.load(f).get("web_enabled", True)
            except Exception: pass

        self.root = tk.Tk()
        self.root.title("Hextech 伴生系统")
        self.root.geometry("320x600")
        self.root.configure(bg="#1e1e2e")
        self.root.attributes('-alpha', 0.85, '-topmost', True)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._init_core_engine()

    def _init_core_engine(self):
        t2 = threading.Thread(target=self.window_sync_loop, daemon=True)
        self.threads.append(t2)
        t2.start()

    def _run_terminal(self):
        pass

    def _build_ui(self):
        self.root.geometry("160x50")
        btn = tk.Button(
            self.root, text="⚔️ Hextech",
            bg="#11111b", fg="#cdd6f4",
            font=("Microsoft YaHei", 11, "bold"),
            relief=tk.FLAT, cursor="hand2",
            command=self.toggle_web
        )
        btn.pack(fill=tk.BOTH, expand=True)
        btn.bind("<ButtonPress-1>", self.start_move)
        btn.bind("<B1-Motion>", self.do_move)
        self.status_label = tk.Label(
            self.root, text="",
            bg="#11111b", fg="#a6adc8",
            font=("Microsoft YaHei", 8)
        )

    def check_and_sync_data(self):
        # 无条件触发后台静默同步，保证每次启动 UI 都是最新数据
        t = threading.Thread(target=self._silent_sync, daemon=True)
        t.start()

    def _silent_sync(self):
        try:
            main_scraper(self.stop_event)
            if self.stop_event.is_set(): return

            new_df = self.load_data()
            def _update_on_main():
                with self._df_lock:
                    self.df = new_df
                if self.status_label.winfo_exists():
                    self.status_label.config(text="✅ 数据同步完成", fg="#a6e3a1")
            self.root.after(0, _update_on_main)
        except Exception: pass

    def load_data(self):
        latest = get_latest_csv()
        if not latest: return pd.DataFrame()
        df = pd.read_csv(latest, dtype={'英雄 ID': str})
        df.columns = df.columns.str.replace(' ', '')  # 暴力清除表头所有空格（包括中间空格）
        df['英雄 ID'] = df['英雄 ID'].str.strip().str.replace('.0', '', regex=False)
        return df

    def on_hero_click(self, champ_id, hero_name):
        try:
            pass
        except Exception:
            pass

    def lcu_polling_loop(self):
        pass

    def _load_and_set_img(self, champ_id, label):
        try:
            pass
        except Exception:
            pass

    def update_ui(self, hero_ids):
        pass

    def window_sync_loop(self):
        while not self.stop_event.is_set():
            if self.pause_event.is_set():
                time.sleep(1); continue
            try:
                hwnd_client = win32gui.FindWindow(None, "League of Legends")
                hwnd_game = win32gui.FindWindow(None, "League of Legends (TM) Client")

                if hwnd_game:
                    self.root.withdraw()
                elif hwnd_client:
                    fg_window = win32gui.GetForegroundWindow()
                    is_client_fg = (fg_window == hwnd_client)
                    is_self_fg = ("Hextech" in win32gui.GetWindowText(fg_window))

                    if is_client_fg or is_self_fg:
                        self.root.deiconify()
                        self.root.attributes('-topmost', True)
                        if not getattr(self, '_init_pos', False) and is_client_fg:
                            rect = win32gui.GetWindowRect(hwnd_client)
                            self.root.geometry(f"320x600+{rect[2]}+{rect[1]}")
                            self._init_pos = True
                    else:
                        self.root.withdraw()
                else:
                    self.root.withdraw()
            except Exception: pass
            time.sleep(0.5)

    def start_move(self, event): self.x, self.y = event.x, event.y
    def do_move(self, event):
        self.root.geometry(f"+{self.root.winfo_x() + (event.x - self.x)}+{self.root.winfo_y() + (event.y - self.y)}")

    def switch_to_query(self, event=None):
        pass

    def _restore_from_terminal(self):
        pass

    def toggle_web(self, event=None):
        """Open browser to access backend (requires manually started backend server)."""
        try:
            webbrowser.open("http://localhost:8000")
            if self.status_label.winfo_exists():
                self.status_label.config(text="已打开浏览器", fg="#a6adc8")
        except Exception as e:
            logging.error(f"打开浏览器失败：{e}")
            if self.status_label.winfo_exists():
                self.status_label.config(text="❌ 打开失败", fg="#f38ba8")

    def on_close(self):
        print("\n[System] 收到退出信号，正在等待数据安全落盘...")
        self.stop_event.set()

        for t in self.threads:
            if t.is_alive():
                t.join(timeout=2)
        self.root.destroy()

if __name__ == "__main__":
    HextechUI().root.mainloop()
