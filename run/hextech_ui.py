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
import sys
import os
import webbrowser
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
        self.start_background_scraper()

    def _init_core_engine(self):
        t1 = threading.Thread(target=self.lcu_polling_loop, daemon=True)
        t2 = threading.Thread(target=self.window_sync_loop, daemon=True)
        t3 = threading.Thread(target=self._run_terminal, daemon=True)
        self.threads.extend([t1, t2, t3])
        for t in self.threads: t.start()

    def _run_terminal(self):
        while not self.stop_event.is_set():
            with self._df_lock:
                is_empty = self.df.empty
            if not is_empty:
                break
            time.sleep(0.5)
        if not self.stop_event.is_set():
            with self._df_lock:
                df_snapshot = self.df
            main_query(shared_df=df_snapshot, ui_instance=self)

    def _build_ui(self):
        self.title_frame = tk.Frame(self.root, bg="#11111b")
        self.title_frame.pack(fill=tk.X)

        self.title_bar = tk.Label(self.title_frame, text="⚔️ 备战席", bg="#11111b", fg="#cdd6f4", font=("Microsoft YaHei", 12, "bold"), pady=8)
        self.title_bar.pack(side=tk.LEFT, padx=(10, 0))
        self.title_bar.bind("<ButtonPress-1>", self.start_move)
        self.title_bar.bind("<B1-Motion>", self.do_move)

        self.switch_btn = tk.Label(self.title_frame, text="[切换终端]", bg="#11111b", fg="#f38ba8", font=("Microsoft YaHei", 10, "bold", "underline"), pady=8, cursor="hand2")
        self.switch_btn.pack(side=tk.RIGHT, padx=(0, 10))
        self.switch_btn.bind("<Button-1>", self.switch_to_query)

        init_text = "[网页:开]" if self.web_enabled else "[网页:关]"
        init_color = "#a6e3a1" if self.web_enabled else "#f38ba8"
        self.web_toggle_btn = tk.Label(self.title_frame, text=init_text, bg="#11111b", fg=init_color, font=("Microsoft YaHei", 10, "bold", "underline"), pady=8, cursor="hand2")
        self.web_toggle_btn.pack(side=tk.RIGHT, padx=(0, 5))
        self.web_toggle_btn.bind("<Button-1>", self.toggle_web)

        self.canvas = tk.Canvas(self.root, bg="#1e1e2e", highlightthickness=0)
        self.list_frame = tk.Frame(self.canvas, bg="#1e1e2e")
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.status_label = tk.Label(self.root, text="系统初始化中...", bg="#1e1e2e", fg="#a6adc8", font=("Microsoft YaHei", 9))
        self.status_label.pack(side=tk.BOTTOM, pady=5)

    def check_and_sync_data(self):
        latest = get_latest_csv()
        today_str = datetime.now().strftime('%Y-%m-%d')
        if not latest or today_str not in latest:
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
        df = pd.read_csv(latest)
        df.columns = df.columns.str.replace(' ', '')
        # 动态查找 ID 列并处理
        id_col = None
        for col in df.columns:
            if '英雄ID' in col or 'ID' in col:
                id_col = col
                break
        if id_col:
            df[id_col] = df[id_col].astype(str).str.strip().str.replace('.0', '', regex=False)
        return df

    def on_hero_click(self, champ_id, hero_name):
        try:
            set_last_hero(hero_name)

            def terminal_task():
                try:
                    sys.stdout.write('\r' + ' ' * 80 + '\r')
                    sys.stdout.flush()
                    with self._df_lock:
                        df_snapshot = self.df
                    display_hero_hextech(df_snapshot, hero_name, is_from_ui=True)
                except Exception as e:
                    print(f"\n❌ 输出错误: {e}")

            threading.Thread(target=terminal_task, daemon=True).start()

            if not getattr(self, 'web_enabled', True): return

            current_time = time.time()
            if current_time - getattr(self, 'last_click_time', 0) < 1.5: return
            self.last_click_time = current_time

            eng_name = self.core_data.get(str(champ_id), {}).get('en_name', '')
            if eng_name:
                url = f"https://apexlol.info/zh/champions/{eng_name}"
                webbrowser.open(url)

            # 融合 HTTP POST /api/redirect 请求
            try:
                requests.post("http://localhost:5000/api/redirect",
                             data={"hero_id": champ_id, "hero_name": hero_name},
                             timeout=3)
            except Exception:
                pass
        except Exception: pass

    def lcu_polling_loop(self):
        while not self.stop_event.is_set():
            if self.pause_event.is_set():
                time.sleep(1)
                continue

            if not self.port:
                for proc in psutil.process_iter(['name', 'cmdline']):
                    try:
                        if proc.info['name'] == 'LeagueClientUx.exe':
                            for arg in proc.info['cmdline'] or []:
                                if arg.startswith('--app-port='): self.port = arg.split('=')[1]
                                if arg.startswith('--remoting-auth-token='): self.token = arg.split('=')[1]
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                time.sleep(2); continue

            headers = {"Authorization": f"Basic {base64.b64encode(f'riot:{self.token}'.encode()).decode()}", "Accept": "application/json"}
            try:
                res = requests.get(f"https://127.0.0.1:{self.port}/lol-champ-select/v1/session", headers=headers, verify=False, timeout=3)
                if res.status_code == 200:
                    data = res.json()
                    available_ids = {str(c['championId']) for c in data.get('benchChampions', [])}
                    for p in data.get('myTeam', []):
                        if p.get('cellId') == data.get('localPlayerCellId') and p.get('championId') != 0:
                            available_ids.add(str(p['championId']))

                    if available_ids != self.current_hero_ids:
                        self.current_hero_ids = available_ids.copy()
                        self.root.after(0, self.update_ui, available_ids)
                else:
                    self.root.after(0, lambda: [w.destroy() for w in self.list_frame.winfo_children()])
            except Exception:
                self.port = None
            time.sleep(1.5)

    def _load_and_set_img(self, champ_id, label):
        try:
            if not label.winfo_exists(): return
            if champ_id in self.image_cache:
                label.config(image=self.image_cache[champ_id]); return

            img_path = os.path.join(ASSET_DIR, f"{champ_id}.png")
            if os.path.exists(img_path):
                img = Image.open(img_path).resize((48, 48), Image.Resampling.LANCZOS)
            else:
                if champ_id in self.downloading_imgs: return # 防止同一头像被重复并发请求
                self.downloading_imgs.add(champ_id)
                url = f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/{champ_id}.png"
                res = self.session.get(url, verify=True, timeout=10)
                if res.status_code == 200:
                    with self.img_write_lock: # 文件锁：防止多线程同时写入导致图片损坏
                        with open(img_path, "wb") as f: f.write(res.content)
                    img = Image.open(BytesIO(res.content)).resize((48, 48), Image.Resampling.LANCZOS)
                else:
                    self.downloading_imgs.discard(champ_id)
                    return
                self.downloading_imgs.discard(champ_id)

            photo = ImageTk.PhotoImage(img)
            self.image_cache[champ_id] = photo
            if label.winfo_exists(): label.config(image=photo)
        except Exception: pass

    def update_ui(self, hero_ids):
        for w in self.list_frame.winfo_children(): w.destroy()

        with self._df_lock:
            is_empty = self.df.empty

        if not hero_ids or is_empty:
            tk.Label(self.list_frame, text="⚠️ 当前无备战英雄，或数据仍在同步中...",
                     fg="#f9e2af", bg="#1e1e2e", font=("Microsoft YaHei", 10)).pack(pady=20)
            return

        self.status_label.config(text="✅ 实时数据已挂载", fg="#a6e3a1")
        display_list = []

        with self._df_lock:
            current_df = self.df

        for hid in hero_ids:
            # 动态查找 ID 列
            id_col = None
            for col in current_df.columns:
                if '英雄ID' in col or 'ID' in col:
                    id_col = col
                    break

            if id_col:
                h_data = current_df[current_df[id_col]==hid]
                if not h_data.empty:
                    row = h_data.iloc[0]
                    # 使用多重回退获取数据
                    name = row.get('英雄名称', row.get('英雄名', '未知'))
                    win = float(row.get('英雄胜率', row.get('胜率', 0.5)))
                    pick = float(row.get('英雄出场率', row.get('出场率', 0.1)))
                    tier = row.get('英雄评级', row.get('评级', 'T?'))

                    display_list.append({
                        'id': hid, 'name': name, 'win': win,
                        'pick': pick, 'tier': tier
                    })

        display_list = sorted(display_list, key=lambda x: x['win'], reverse=True)

        for item in display_list:
            card = tk.Frame(self.list_frame, bg="#313244", pady=5, padx=5, cursor="hand2")
            card.pack(fill=tk.X, pady=4, padx=(0, 10))

            img_label = tk.Label(card, bg="#313244")
            img_label.pack(side=tk.LEFT, padx=(0, 10))
            threading.Thread(target=lambda i=item['id'], l=img_label: self._load_and_set_img(i, l), daemon=True).start()

            info = tk.Frame(card, bg="#313244")
            info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            title = self.core_data.get(item['id'], {}).get('title', '')
            full_name = f"{item['name']} {title}".strip() if title else item['name']

            tk.Label(info, text=f"[{item['tier']}] {full_name}", font=("Microsoft YaHei", 10, "bold"), fg="#cdd6f4", bg="#313244").pack(anchor="w")
            tk.Label(info, text=f"胜率: {item['win']:.1%} | 出场: {item['pick']:.1%}", font=("Microsoft YaHei", 9), fg="#a6adc8", bg="#313244").pack(anchor="w", pady=(3, 0))

            bar_canvas = tk.Canvas(info, height=4, bg="#1e1e2e", highlightthickness=0)
            bar_canvas.pack(fill=tk.X, pady=(4, 0))
            bar_color = "#a6e3a1" if item['win'] >= 0.51 else ("#f9e2af" if item['win'] >= 0.48 else "#f38ba8")
            ratio = max(0, min(1, (item['win'] - 0.40) / 0.20))

            bar_canvas.bind("<Configure>", lambda e, c=bar_canvas, r=ratio, col=bar_color:
                            (c.delete("all"), c.create_rectangle(0, 0, int(r * e.width), 4, fill=col, outline="")))

            def bind_click(widget, cid, name):
                widget.bind("<Button-1>", lambda e, c=cid, n=name: self.on_hero_click(c, n))
                for child in widget.winfo_children(): bind_click(child, cid, name)
            bind_click(card, item['id'], item['name'])

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
        if self.pause_event.is_set(): return
        print("\n[System] 已隐藏悬浮窗。终端引擎常驻运行中，按 'u' 回车即可恢复界面。")
        self.pause_event.set()
        self.root.withdraw()

    def _restore_from_terminal(self):
        self.pause_event.clear()
        self.root.deiconify()
        self.root.attributes('-topmost', True)

    def toggle_web(self, event=None):
        self.web_enabled = not self.web_enabled
        self.web_toggle_btn.config(text="[网页:开]" if self.web_enabled else "[网页:关]", fg="#a6e3a1" if self.web_enabled else "#f38ba8")

        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            with open(self.settings_file, "w") as f:
                json.dump({"web_enabled": self.web_enabled}, f)
        except Exception as e:
            print(f"\n⚠️ 配置保存失败: {e}")

    def start_background_scraper(self):
        """启动 4 小时循环的后台抓取守护线程"""
        def scraper_loop():
            while not self.stop_event.is_set():
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

                # 等待 4 小时 (14400 秒)
                for _ in range(14400):
                    if self.stop_event.is_set():
                        break
                    time.sleep(1)

        scraper_thread = threading.Thread(target=scraper_loop, daemon=True)
        scraper_thread.start()

    def on_close(self):
        print("\n[System] 收到退出信号，正在等待数据安全落盘...")
        self.stop_event.set()
        for t in self.threads:
            if t.is_alive():
                t.join(timeout=2)
        self.root.destroy()

if __name__ == "__main__":
    HextechUI().root.mainloop()