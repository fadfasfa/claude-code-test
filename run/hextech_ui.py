"""
Hextech 伴生系统 - Tkinter 桌面悬浮窗版本 V5.2
功能：
  - 桌面悬浮窗（无边框、透明背景、置顶）
  - 英雄列表显示与点击跳转
  - 4 小时后台守护抓取任务
  - Web 端重定向 API 对接
"""
import json
import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, font
import pandas as pd
import numpy as np
import math
from pathlib import Path
from datetime import datetime, timedelta
from hero_sync import BASE_DIR, ASSET_DIR

Path(ASSET_DIR).mkdir(parents=True, exist_ok=True)

try:
    from hextech_query import get_latest_csv, display_hero_hextech, main_query, set_last_hero
    from hero_sync import load_champion_core_data, get_advanced_session
    from hextech_scraper import main_scraper
except ImportError:
    print("❌ 缺少核心依赖模块，请确保文件结构完整。")
    exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Path(BASE_DIR) / "config" / "hextech_system.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("HextechUI")


class HextechUI:
    """Tkinter 桌面悬浮窗 UI 类"""

    def __init__(self):
        self.session = get_advanced_session()
        self.core_data = load_champion_core_data()
        self.df = self.load_data()
        self.hero_synergy_data = self.load_synergy_data()
        self.running = True
        self.last_hero = None
        
        # 初始化 Tkinter 根窗口
        self.root = tk.Tk()
        self.root.title("Hextech 伴生系统")
        self.root.overrideredirect(True)  # 无边框
        self.root.attributes('-topmost', True)  # 置顶
        self.root.attributes('-alpha', 0.9)  # 透明度
        
        # 设置窗口位置（右上角）
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = 320
        window_height = 480
        x = screen_width - window_width - 10
        y = 10
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        
        # 设置背景色（支持透明）
        self.root.configure(bg='#1a1a2e')
        self.root.wm_attributes('-transparentcolor', '#1a1a2e')
        
        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # 绑定拖拽事件
        self.root.bind('<Button-1>', self.start_move)
        self.root.bind('<B1-Motion>', self.do_move)
        
        # 创建主框架
        self.main_frame = tk.Frame(self.root, bg='#16213e', bd=2, relief='ridge')
        self.main_frame.pack(fill='both', expand=True, padx=5, pady=5)
        
        # 标题栏
        self.title_label = tk.Label(
            self.main_frame, text="🔮 Hextech 伴生系统",
            bg='#0f3460', fg='#e94560',
            font=('Microsoft YaHei UI', 12, 'bold')
        )
        self.title_label.pack(fill='x', pady=(0, 5))
        
        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = tk.Label(
            self.main_frame, textvariable=self.status_var,
            bg='#0f3460', fg='#00ff88',
            font=('Microsoft YaHei UI', 9)
        )
        self.status_label.pack(fill='x', pady=(0, 5))
        
        # 英雄列表框架
        self.list_frame = tk.Frame(self.main_frame, bg='#1a1a2e')
        self.list_frame.pack(fill='both', expand=True, padx=5, pady=5)
        
        # 创建滚动条
        self.scrollbar = ttk.Scrollbar(self.list_frame)
        self.scrollbar.pack(side='right', fill='y')
        
        # 创建英雄列表
        self.hero_listbox = tk.Listbox(
            self.list_frame,
            bg='#16213e',
            fg='#ffffff',
            selectbackground='#e94560',
            selectforeground='#ffffff',
            font=('Microsoft YaHei UI', 10),
            yscrollcommand=self.scrollbar.set,
            border=0,
            highlightthickness=0
        )
        self.hero_listbox.pack(side='left', fill='both', expand=True)
        self.scrollbar.config(command=self.hero_listbox.yview)
        
        # 绑定点击事件
        self.hero_listbox.bind('<<ListboxSelect>>', self.on_hero_select)
        
        # 填充英雄列表
        self.populate_hero_list()
        
        # 启动后台守护任务
        self.start_background_scraper()
        
        logger.info("HextechUI 初始化完成")

    def load_data(self):
        """加载 CSV 数据到 DataFrame"""
        latest = get_latest_csv()
        if not latest:
            return pd.DataFrame()
        df = pd.read_csv(latest)
        df.columns = df.columns.str.replace(' ', '')
        
        # 动态容错：获取去除了空格的 ID 列名
        id_col = '英雄 ID' if '英雄 ID' in df.columns else '英雄 ID'
        if id_col in df.columns:
            df[id_col] = df[id_col].astype(str).str.strip().str.replace('.0', '', regex=False)
        return df

    def load_synergy_data(self):
        """从 JSON 文件加载英雄联动数据"""
        synergy_file = Path(BASE_DIR) / "config" / "Champion_Synergy.json"
        try:
            with open(synergy_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def populate_hero_list(self):
        """填充英雄列表"""
        self.hero_listbox.delete(0, 'end')
        if self.df.empty:
            self.hero_listbox.insert('end', "暂无数据")
            return
        
        id_col = '英雄 ID' if '英雄 ID' in self.df.columns else '英雄 ID'
        name_col = '英雄名' if '英雄名' in self.df.columns else '英雄名'
        
        for _, row in self.df.iterrows():
            hero_id = row.get(id_col, '')
            hero_name = row.get(name_col, '')
            display_text = f"{hero_id} - {hero_name}"
            self.hero_listbox.insert('end', display_text)

    def on_hero_select(self, event):
        """处理英雄选择事件"""
        selection = self.hero_listbox.curselection()
        if not selection:
            return
        
        index = selection[0]
        display_text = self.hero_listbox.get(index)
        
        # 解析英雄 ID 和名称
        parts = display_text.split(' - ', 1)
        if len(parts) >= 2:
            hero_id = parts[0].strip()
            hero_name = parts[1].strip()
            self.last_hero = hero_name
            set_last_hero(hero_name)
            self.status_var.set(f"已选择：{hero_name}")
            
            # 发送重定向请求到 Web 端
            self.send_redirect_request(hero_id, hero_name)
            
            # 显示海克斯信息
            try:
                info = display_hero_hextech(self.df, hero_name)
                if info:
                    logger.info(f"英雄 {hero_name} 海克斯信息：{info}")
            except Exception as e:
                logger.error(f"获取海克斯信息失败：{e}")

    def send_redirect_request(self, hero_id, hero_name):
        """向 Web 端发送重定向请求"""
        import urllib.request
        import urllib.parse
        
        try:
            url = "http://localhost:5000/api/redirect"
            data = urllib.parse.urlencode({
                'hero_id': hero_id,
                'hero_name': hero_name
            }).encode('utf-8')
            
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            
            with urllib.request.urlopen(req, timeout=5) as response:
                result = response.read().decode('utf-8')
                logger.info(f"重定向请求成功：{result}")
                self.status_var.set(f"已跳转：{hero_name}")
        except Exception as e:
            logger.warning(f"Web 端不可用，跳过重定向：{e}")

    def start_background_scraper(self):
        """启动后台守护抓取任务（4 小时间隔）"""
        def scraper_loop():
            while self.running:
                try:
                    logger.info("开始后台抓取任务...")
                    main_scraper()
                    self.df = self.load_data()
                    self.populate_hero_list()
                    self.status_var.set("数据已更新")
                    logger.info("后台抓取任务完成")
                except Exception as e:
                    logger.error(f"后台抓取任务失败：{e}")
                    self.status_var.set("抓取失败")
                
                # 等待 4 小时
                for _ in range(240):  # 240 分钟 = 4 小时
                    if not self.running:
                        break
                    time.sleep(60)  # 每分钟检查一次
        
        self.scraper_thread = threading.Thread(target=scraper_loop, daemon=True)
        self.scraper_thread.start()
        logger.info("后台守护任务已启动（4 小时间隔）")

    def start_move(self, event):
        """开始拖拽窗口"""
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        """拖拽窗口"""
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def on_close(self):
        """清理资源并关闭窗口"""
        self.running = False
        logger.info("HextechUI 关闭")
        self.root.destroy()


def main():
    """主入口函数"""
    ui = HextechUI()
    logger.info(f"已加载 {len(ui.df)} 条英雄数据")
    logger.info(f"已加载 {len(ui.hero_synergy_data)} 条联动数据")
    ui.root.mainloop()


if __name__ == "__main__":
    main()
