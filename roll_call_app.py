import tkinter as tk
import sys
import os
import io

# 打包后 __file__ 指向 PyInstaller 临时解包目录，数据库必须放在 exe 同目录
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 添加tools目录到Python路径
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))

from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timedelta
import sqlite3
import re
import pickle
import configparser
import threading
import requests
import hashlib
import time
import csv
import socket
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
# 用于PDF导出的库
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
# 用于JPEG导出的库
from PIL import Image, ImageDraw, ImageFont
# 用于DOCX导出的库
import docx

# 导入抓取盒子工具模块
from tools import pi_star_http_test

# 导入客户端更新模块
try:
    from client_updater import ClientUpdater
except ImportError:
    # 如果client_updater不存在，使用简化版本
    ClientUpdater = None

# 导入 P2P 同步模块（可选，缺失时不影响主程序）
try:
    from p2p_sync import P2PSyncEngine
except ImportError:
    P2PSyncEngine = None


# 尝试从配置文件读取API_BASE_URL
def load_api_base_url():
    """从配置文件读取API基础URL，如果不存在则使用默认值"""
    config = configparser.ConfigParser()
    config_file = "app_config.ini"
    default_url = 'http://bh2vlf.cn:4567'
    
    if os.path.exists(config_file):
        config.read(config_file, encoding='utf-8')
        if "Settings" in config and "api_base_url" in config["Settings"]:
            return config["Settings"]["api_base_url"]
    
    # 如果配置文件不存在或没有设置API URL，保存默认值
    if not os.path.exists(config_file):
        config["Settings"] = {"api_base_url": default_url}
        with open(config_file, "w") as f:
            config.write(f)
    elif "Settings" not in config:
        config["Settings"] = {"api_base_url": default_url}
        with open(config_file, "w") as f:
            config.write(f)
    
    return default_url

# 服务器API基础URL
API_BASE_URL = load_api_base_url()

# 客户端版本号
CLIENT_VERSION = "2.1"

class RollCallApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"业余无线电台网点名软件 v{CLIENT_VERSION}")
        self.root.geometry("1200x600")
        self.root.minsize(1024, 768)
        
        # 点击窗口关闭按钮时与“结束”按钮功能一致
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        
        # 创建菜单
        self.create_menu()
        
        # 初始化数据库
        self.init_database()
        
        # P2P 同步引擎（可选，按 app_config.ini 的 [P2P] 配置启动）
        self.p2p_engine = None
        self.p2p_status = {}
        self.init_p2p_sync()
        
        # 抓取参数（最短通话时长/最近通话窗口），可由主控通过“抓取参数”修改
        self.grab_min_duration = 3
        self.grab_window_minutes = 3
        self._load_grab_settings()
        
        # 呼号库与中继的汇聚同步（可选，依赖 [P2P] relay_url；确认台网后按主控呼号库启动）
        self.master_lib_sync = None
        self.master_lib_sync_target = ""
        
        # 初始化城市数据
        self.init_city_data()
        
        # 设置中文字体支持
        self.style = ttk.Style()
        self.style.configure("Treeview.Heading", font=("SimHei", 10, "bold"))
        self.style.configure("Treeview", font=("SimHei", 10))
        self.style.configure("TLabel", font=("SimHei", 10))
        self.style.configure("TButton", font=("SimHei", 10))
        self.style.configure("TEntry", font=("SimHei", 10))
        
        # 为确认时间列创建特殊样式（放大字体）
        self.style.configure("Time.Treeview.Cell", font=("SimHei", 12, "bold"))
        
        # 数据存储
        self.records = []
        # 手机点名（中继）会话
        self.mobile_batch_id = ""
        self.mobile_last_id = 0
        self.mobile_polling = False
        self.mobile_poll_interval = 15000  # 毫秒
        self.start_rank = 1  # 自定义起始名次
        self.current_rank = self.start_rank
        self.qth_pinyin_converting = False  # 防止转换循环的标志
        self.qth_updating = False  # 防止QTH更新循环的标志
        self.qth_dropdown = None  # QTH下拉选择框
        self.updating_call_sign = False  # 防止呼号更新循环的标志
        self.updating_access_method = False  # 防止接入方式更新循环的标志
        self.updating_operator_call_sign = False  # 防止主控呼号更新循环的标志
        
        # 接入方式映射（数字到文本）
        self.access_method_map = {
            "1": "热点盒子",
            "2": "数字中继",
            "3": "ROIP直通",
            "4": "软件接入",
            "5": "模拟中继",
            "6": "模拟直频",
            "7": "短波CW",
            "8": "短波SSB",
            "9": "短波FM" 
        }
        
        # 创建主框架
        self.main_frame = ttk.Frame(root, padding="10")
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建台网点名输入区域
        self.create_station_name_frame()
        
        # 创建输入区域
        self.create_input_frame()
        
        # 创建显示区域
        self.create_display_frame()
        
        # 创建按钮区域
        self.create_button_frame()
        
        # 创建历史记录提示框
        self.history_tooltip = ttk.Label(
            self.main_frame, 
            text="", 
            font=("SimHei", 9),
            foreground="#666666",
            justify=tk.LEFT
        )
        self.history_tooltip.pack(fill=tk.X, pady=2)
        
        # 添加版权信息（新增）
        self.copyright_label = ttk.Label(
            self.main_frame, 
            text="版权及开发 BH2VLF", 
            font=("SimHei", 9, "italic"),
            foreground="#888888",
            cursor="hand2"  # 鼠标悬停时显示手型光标
        )
        self.copyright_label.pack(side=tk.BOTTOM, pady=5)
        
        # P2P 同步状态指示灯（点击打开详细状态窗口）
        p2p_bar = ttk.Frame(self.main_frame)
        p2p_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.p2p_status_canvas = tk.Canvas(p2p_bar, width=16, height=16, highlightthickness=0,
                                           bg=ttk.Style().lookup("TFrame", "background"))
        self.p2p_status_canvas.create_oval(2, 2, 14, 14, fill="#888888", outline="", tags="dot")
        self.p2p_status_canvas.pack(side=tk.LEFT, padx=(6, 6))
        self.p2p_status_label = ttk.Label(
            p2p_bar, text="P2P: 未启用",
            font=("SimHei", 9), foreground="#888888", cursor="hand2")
        self.p2p_status_label.pack(side=tk.LEFT)
        self.p2p_status_canvas.bind("<Button-1>", lambda e: self.show_p2p_status())
        self.p2p_status_label.bind("<Button-1>", lambda e: self.show_p2p_status())
        # 初始刷新一次
        self._update_p2p_status_light()
        
        # 初始化点击计数器
        self.copyright_click_count = 0
        
        # 绑定点击事件
        self.copyright_label.bind("<Button-1>", self.on_copyright_click)
    
    def create_station_name_frame(self):
        """创建台网点名输入区域"""
        station_frame = ttk.LabelFrame(self.main_frame, text="台网信息", padding="10")
        station_frame.pack(fill=tk.X, pady=5)
        
        # 台网点名输入
        ttk.Label(station_frame, text="台网点名:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.station_name_var = tk.StringVar()
        self.station_name_entry = ttk.Entry(station_frame, textvariable=self.station_name_var, width=30)
        self.station_name_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)
        
        # 主控呼号输入
        ttk.Label(station_frame, text="主控呼号:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        self.operator_call_sign_var = tk.StringVar()
        self.operator_call_sign_entry = ttk.Entry(station_frame, textvariable=self.operator_call_sign_var, width=15)
        self.operator_call_sign_entry.grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)
        
        # 绑定主控呼号输入事件，实现实时大写转换
        self.operator_call_sign_var.trace_add("write", self.on_operator_call_sign_change)
        
        # 台网类型单选按钮
        self.network_type_var = tk.StringVar(value="模拟台网")  # 默认选择模拟台网
        digital_radio_button = ttk.Radiobutton(station_frame, text="数字台网", variable=self.network_type_var, value="数字台网")
        digital_radio_button.grid(row=0, column=4, padx=5, pady=5, sticky=tk.W)
        
        analog_radio_button = ttk.Radiobutton(station_frame, text="模拟台网", variable=self.network_type_var, value="模拟台网")
        analog_radio_button.grid(row=0, column=5, padx=5, pady=5, sticky=tk.W)
        
        # 确认按钮
        self.confirm_button = ttk.Button(station_frame, text="确认", command=self.on_confirm, width=10)
        self.confirm_button.grid(row=0, column=6, padx=5, pady=5, sticky=tk.W)
        
        # 设置起始名次按钮
        self.set_start_rank_button = ttk.Button(station_frame, text="设置起始名次", command=self.set_start_rank, width=12)
        self.set_start_rank_button.grid(row=0, column=7, padx=5, pady=5, sticky=tk.W)
        
        # 设置焦点到台网点名输入框
        self.station_name_entry.focus()
        
        # 绑定回车键跳转到主控呼号输入
        self.station_name_entry.bind('<Return>', lambda event: self.operator_call_sign_entry.focus())
        # 绑定主控呼号输入框回车键到确认按钮
        self.operator_call_sign_entry.bind('<Return>', lambda event: self.on_confirm())
        
        # 监听台网类型变化，控制备选框的显示/隐藏
        self.network_type_var.trace_add("write", self.on_network_type_change)
        
        # 创建抓取盒子区域
        self.create_digital_network_frame()
        

    



    

        
    def on_network_type_change(self, *args):
        """台网类型变化时的处理函数"""
        network_type = self.network_type_var.get()
        
        # 模拟台网状态下，天馈参数可编辑
        if hasattr(self, 'antenna_entry'):
            self.antenna_entry.config(state='normal' if network_type == "模拟台网" else 'disabled')
        
        # 更新接入方式列表显示
        if hasattr(self, 'access_method_label'):
            if network_type == "模拟台网":
                self.access_method_label.config(
                    text="接入方式列表: 4-软件接入, 5-模拟中继, 6-模拟直频, 7-短波CW, 8-短波SSB, 9-短波FM"
                )
            else:
                self.access_method_label.config(
                    text="接入方式列表: 1-热点盒子, 2-数字中继, 3-ROIP直通, 4-软件接入"
                )
        
        # 控制抓取盒子区域的显示/隐藏
        if hasattr(self, 'digital_network_frame'):
            if network_type == "数字台网":
                # 确保抓取盒子区域在台网信息和输入信息之间
                # 先确保框架被显示
                if self.digital_network_frame.winfo_manager() == '':
                    # 获取主框架中的所有子组件
                    children = list(self.main_frame.children.values())
                    # 查找输入框架
                    input_frame = None
                    
                    for child in children:
                        if hasattr(self, 'input_frame') and child == self.input_frame:
                            input_frame = child
                    
                    # 如果找到了输入框架，就在它之前放置抓取盒子区域
                    if input_frame:
                        self.digital_network_frame.pack(fill=tk.X, pady=5, before=input_frame)
                    else:
                        # 否则就正常放置
                        self.digital_network_frame.pack(fill=tk.X, pady=5)
            else:
                self.digital_network_frame.pack_forget()
        

        
    def on_confirm(self):
        """确认按钮点击事件处理"""
        station_name = self.station_name_var.get().strip()
        operator_call_sign = self.operator_call_sign_var.get().strip()
        network_type = self.network_type_var.get()  # 获取台网类型
        
        if not station_name:
            messagebox.showwarning("警告", "请输入台网点名")
            return
        
        if not operator_call_sign:
            messagebox.showwarning("警告", "请输入主控呼号")
            return
        
        # 创建一个以主控呼号为名字的数据库，并生成call_sign_library表
        db_status = ""
        try:
            # 创建数据库连接 (以主控呼号为文件名)
            db_filename = os.path.join(APP_DIR, f"{operator_call_sign}.db")
            # 连接数据库，若不存在则创建
            operator_conn = sqlite3.connect(db_filename)
            operator_cursor = operator_conn.cursor()
            
            # 创建呼号库表
            operator_cursor.execute('''
            CREATE TABLE IF NOT EXISTS call_sign_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sign TEXT UNIQUE NOT NULL,
                qth TEXT,
                equipment TEXT,
                power TEXT,
                antenna TEXT,
                access_method TEXT
            )
            ''')
            
            # 创建设备表（walkie_talkies）- 只包含品牌和型号字段
            operator_cursor.execute('''
            CREATE TABLE IF NOT EXISTS walkie_talkies (
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                PRIMARY KEY (brand, model)
            )
            ''')
            
            # 提交更改
            operator_conn.commit()
            
            # 从 call_sign_history.db 同步设备表数据
            try:
                history_conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                history_cursor = history_conn.cursor()
                
                # 获取所有设备数据
                history_cursor.execute('SELECT DISTINCT brand, model FROM walkie_talkies')
                walkie_data = history_cursor.fetchall()
                history_conn.close()
                
                if walkie_data:
                    print(f"从 call_sign_history.db 读取了 {len(walkie_data)} 条设备记录。")
                    # 将数据插入到主控数据库
                    operator_cursor.executemany('''
                        INSERT OR IGNORE INTO walkie_talkies (brand, model)
                        VALUES (?, ?)
                    ''', walkie_data)
                    operator_conn.commit()
                    print(f"成功同步 {len(walkie_data)} 条设备记录到 {db_filename}")
                    db_status = f"已创建上传数据库: {db_filename}\n并生成所需表结构\n同步了{len(walkie_data)}条设备记录\n\n"
                else:
                    print("未从 call_sign_history.db 读取到设备记录")
                    db_status = f"已创建上传数据库: {db_filename}\n并生成所需表结构\n但未找到可同步的设备记录\n\n"
            except sqlite3.Error as e:
                print(f"同步设备表失败: {e}")
                db_status = f"已创建上传数据库: {db_filename}\n并生成所需表结构\n但设备表同步失败: {e}\n\n"
            finally:
                # 完成后关闭连接
                operator_conn.close()
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"创建上传数据库失败: {str(e)}")
            # 即使数据库创建失败，仍然继续显示确认信息
        
        # 呼号库汇聚：把当前主控呼号库接入中继同步
        self.start_library_sync(operator_call_sign)

        # 手机点名：在中继开启当前点名批次
        self.start_mobile_session(operator_call_sign, station_name)
        
        # 更新主控行
        self.update_master_row()
        
        # 合并提示信息，包含台网类型
        messagebox.showinfo("确认成功", f"{db_status}台网点名: {station_name}\n主控呼号: {operator_call_sign}\n台网类型: {network_type}\n已确认")
        
        # 跳转到呼号输入
        if hasattr(self, 'call_sign_entry'):
            self.call_sign_entry.focus()
        
    def init_city_data(self):
        """初始化全国城市数据，优先从缓存文件加载，缓存不存在时从数据库加载"""
        # 首先尝试从缓存文件加载数据
        try:
            if os.path.exists('city_data.pkl'):
                with open('city_data.pkl', 'rb') as f:
                    cached_data = pickle.load(f)
                    self.provinces = cached_data['provinces']
                    self.city_list = cached_data['city_list']
                    self.city_pinyin_map = cached_data['city_pinyin_map']
                    # 加载区县映射
                    if 'district_map' in cached_data:
                        self.district_map = cached_data['district_map']
                    else:
                        self.district_map = {}
                    
                    # 检查district_map是否为空，如果为空则继续从数据库加载
                    if not self.district_map or len(self.district_map) == 0:
                        print("缓存中的区县数据为空，继续从数据库加载")
                    else:
                        print(f"成功从缓存加载了{len(self.provinces)}个省份, {sum(len(cities) for cities in self.city_list.values())}个城市和{sum(len(districts) for districts in self.district_map.values())}个区县")
                        return  # 成功加载缓存后直接返回
        except Exception as e:
            print(f"从缓存加载城市数据失败: {str(e)}")
            # 缓存加载失败，继续从数据库加载
            self.district_map = {}  # 初始化区县映射
        
        try:
            # 定义特殊城市映射（在方法开始就定义，确保在任何地方都能访问）
            special_mappings = {
                "beijing": "北京市",
                "bj": "北京市",
                "shanghai": "上海市",
                "sh": "上海市",
                "tianjin": "天津市",
                "tj": "天津市",
                "chongqing": "重庆市",
                "cq": "重庆市"
            }
            
            # 从数据库加载城市数据
            self.cursor.execute('SELECT DISTINCT "省" FROM qth_data')
            provinces = self.cursor.fetchall()
            self.provinces = [p[0] for p in provinces if p[0]]
            
            # 初始化城市列表和区县映射
            self.city_list = {}
            self.district_map = {}
            
            for province in self.provinces:
                self.cursor.execute('SELECT DISTINCT "市" FROM qth_data WHERE "省" = ?', (province,))
                cities = self.cursor.fetchall()
                self.city_list[province] = [c[0] for c in cities if c[0]]
                
                # 初始化区县映射
                for city in self.city_list[province]:
                    self.cursor.execute('SELECT DISTINCT "省/市/区" FROM qth_data WHERE "省" = ? AND "市" = ?', (province, city))
                    districts = self.cursor.fetchall()
                    self.district_map[(province, city)] = [d[0] for d in districts if d[0]]
            
            # 初始化城市拼音映射表
            self.city_pinyin_map = {}
            
            # 获取所有城市数据，包括拼音信息
            self.cursor.execute('SELECT "市", "首字母" FROM qth_data')
            city_data = self.cursor.fetchall()
            
            for city, initials in city_data:
                if city:
                    # 移除"市"、"自治州"、"地区"等后缀
                    city_base = city.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                    
                    # 添加完整拼音映射
                    self.city_pinyin_map[city_base] = city
                    
                    # 添加首字母映射（如果有），确保首字母映射优先级更高
                    if initials:
                        # 确保首字母映射不会被其他映射覆盖
                        if initials.lower() in self.city_pinyin_map:
                            # 检查现有值是否为特殊映射，如果不是则替换为当前城市
                            if self.city_pinyin_map[initials.lower()] not in special_mappings.values():
                                self.city_pinyin_map[initials.lower()] = city
                        else:
                            self.city_pinyin_map[initials.lower()] = city
                            
                    # 针对有多个汉字的城市，提取每个汉字的声母组合作为额外的映射
                    if len(city_base) > 1:
                        try:
                            # 尝试从城市名称提取声母
                            # 这里假设有一个简单的声母提取方法
                            # 简单实现：取每个汉字拼音的第一个字母
                            # 注意：这是一个简化实现，实际应用中可能需要更复杂的拼音转换库
                            syllables = re.findall(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+', city_base)
                            if syllables:
                                combined_initials = ''.join([s[0] for s in syllables])
                                if combined_initials and combined_initials.lower() not in self.city_pinyin_map:
                                    self.city_pinyin_map[combined_initials.lower()] = city
                        except:
                            pass  # 提取失败时忽略
            
            # 添加一些常用的特殊城市映射（如果数据库中没有）
            for pinyin, city in special_mappings.items():
                if pinyin not in self.city_pinyin_map:
                    self.city_pinyin_map[pinyin] = city
            
            print(f"成功从数据库加载了{len(self.provinces)}个省份, {sum(len(cities) for cities in self.city_list.values())}个城市和{sum(len(districts) for districts in self.district_map.values())}个区县")
        except sqlite3.Error as e:
            print(f"从数据库加载城市数据失败: {str(e)}")
            # 如果数据库加载失败，使用最小化的默认数据
            self.provinces = ["北京市", "上海市", "广州市", "深圳市"]
            self.city_list = {
                "北京市": ["北京市"],
                "上海市": ["上海市"],
                "广州市": ["广州市"],
                "深圳市": ["深圳市"]
            }
            self.city_pinyin_map = {
                "beijing": "北京市",
                "bj": "北京市",
                "shanghai": "上海市",
                "sh": "上海市",
                "guangzhou": "广州市",
                "gz": "广州市",
                "shenzhen": "深圳市",
                "sz": "深圳市"
            }
            self.district_map = {}  # 初始化区县映射
        except Exception as e:
            print(f"初始化城市数据时发生错误: {str(e)}")
            # 极端情况下的最小化默认数据
            self.provinces = ["北京市"]
            self.city_list = {"北京市": ["北京市"]}
            self.city_pinyin_map = {
                "beijing": "北京市", 
                "bj": "北京市"
            }
            self.district_map = {}  # 初始化区县映射
        
        # 自动补全剩余城市的拼音映射
        for province, cities in self.city_list.items():
            for city in cities:
                if city not in self.city_pinyin_map.values():
                    city_pinyin = city.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                    self.city_pinyin_map[city_pinyin] = city
        
        # 缓存城市数据
        try:
            with open('city_data.pkl', 'wb') as f:
                pickle.dump({
                    'provinces': self.provinces,
                    'city_list': self.city_list,
                    'city_pinyin_map': self.city_pinyin_map,
                    'district_map': self.district_map
                }, f)
        except:
            pass  # 缓存失败不影响程序运行
    
    def init_database(self):
        """初始化SQLite数据库（本地数据库），包含功率字段"""
        try:
            # 连接本地数据库，若不存在则创建
            self.history_db_path = os.path.join(APP_DIR, 'call_sign_history.db')
            self.conn = sqlite3.connect(self.history_db_path)
            self.cursor = self.conn.cursor()
            
            # 检查admin表是否存在
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin'")
            table_exists = self.cursor.fetchone()
            
            if not table_exists:
                # 创建管理员表，存储管理员密码（使用哈希加密）
                # 注意：这里不添加NOT NULL约束，以确保兼容性
                self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    password_hash TEXT,
                    password TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''')
                
                # 初始化管理员密码为 'admin123'，并进行哈希加密
                password_hash = hashlib.sha256('admin123'.encode()).hexdigest()
                # 只存储哈希密码，不存储明文密码
                self.cursor.execute("INSERT INTO admin (password_hash) VALUES (?)", (password_hash,))
            else:
                # 检查表中是否有password_hash列
                self.cursor.execute("PRAGMA table_info(admin)")
                columns = [column[1] for column in self.cursor.fetchall()]
                
                # 如果没有password_hash列，添加password_hash列
                if 'password_hash' not in columns:
                    self.cursor.execute("ALTER TABLE admin ADD COLUMN password_hash TEXT")
                    
                    # 如果有password列，读取现有明文密码并转换为哈希值
                    if 'password' in columns:
                        self.cursor.execute("SELECT id, password FROM admin")
                        old_passwords = self.cursor.fetchall()
                        
                        for row in old_passwords:
                            if row[1]:  # 如果密码不为空
                                password_hash = hashlib.sha256(row[1].encode()).hexdigest()
                                self.cursor.execute("UPDATE admin SET password_hash = ? WHERE id = ?", 
                                                  (password_hash, row[0]))
                
                # 如果没有password列，添加password列（允许NULL值）
                if 'password' not in columns:
                    self.cursor.execute("ALTER TABLE admin ADD COLUMN password TEXT")
            
            # 检查是否已有管理员密码记录，如果没有则初始化
            self.cursor.execute("SELECT COUNT(*) FROM admin")
            count = self.cursor.fetchone()[0]
            if count == 0:
                # 初始化管理员密码为 'admin123'，并进行哈希加密
                password_hash = hashlib.sha256('admin123'.encode()).hexdigest()
                self.cursor.execute("INSERT INTO admin (password_hash) VALUES (?)", (password_hash,))
            
            # 创建呼号历史表，包含功率字段
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS call_signs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sign TEXT UNIQUE NOT NULL,
                qth TEXT,
                equipment TEXT,
                power TEXT,  -- 功率字段
                access_method TEXT,
                main_control TEXT,  -- 主控呼号字段
                station_network TEXT,  -- 台网字段
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 如果表已存在，添加功率字段（兼容旧版本）
            try:
                self.cursor.execute("ALTER TABLE call_signs ADD COLUMN power TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 如果表已存在，添加主控字段（兼容旧版本）
            try:
                self.cursor.execute("ALTER TABLE call_signs ADD COLUMN main_control TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 如果表已存在，添加台网字段（兼容旧版本）
            try:
                self.cursor.execute("ALTER TABLE call_signs ADD COLUMN station_network TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 创建点名记录表，包含功率字段
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS roll_call_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rank INTEGER NOT NULL,
                call_sign TEXT NOT NULL,
                qth TEXT,
                equipment TEXT,
                power TEXT,  -- 功率字段
                access_method TEXT,
                confirm_time TIMESTAMP NOT NULL
            )
            ''')
            
            # 如果表已存在，添加功率字段（兼容旧版本）
            try:
                self.cursor.execute("ALTER TABLE roll_call_records ADD COLUMN power TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 创建呼号库表
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS call_sign_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sign TEXT UNIQUE NOT NULL,
                qth TEXT,
                equipment TEXT,
                power TEXT,
                antenna TEXT
            )
            ''')
            
            # 如果表已存在，添加天馈参数字段（兼容旧版本）
            try:
                self.cursor.execute("ALTER TABLE call_sign_library ADD COLUMN antenna TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            try:
                self.cursor.execute("ALTER TABLE call_sign_library ADD COLUMN access_method TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 如果表已存在，为call_signs表添加天馈参数字段
            try:
                self.cursor.execute("ALTER TABLE call_signs ADD COLUMN antenna TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            # 如果表已存在，为roll_call_records表添加天馈参数字段
            try:
                self.cursor.execute("ALTER TABLE roll_call_records ADD COLUMN antenna TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            self.conn.commit()
            
            # 在初始化数据库后，检查并同步主数据库到客户端数据库
            self.sync_master_database_on_login()
            
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"初始化数据库失败: {str(e)}")
    
    def sync_master_database_on_login(self):
        """在软件登录时同步主数据库到客户端数据库"""
        try:
            # 检查主数据库是否存在
            master_db_path = os.path.join(APP_DIR, "master_call_sign_library.db")
            if not os.path.exists(master_db_path):
                print("主数据库不存在，跳过同步")
                return
            
            # 调用现有的同步函数
            sync_downloaded_database(master_db_path)
            
        except Exception as e:
            print(f"登录时同步数据库失败: {str(e)}")
    
    def get_admin_password(self):
        """获取管理员密码（明文，用于API调用）"""
        # 这里需要从数据库获取明文密码，但数据库中存储的是hash
        # 为了保持兼容性，暂时返回默认密码
        # 在实际应用中，应该有一个密码管理界面来设置密码
        return 'admin123'
    
    def create_input_frame(self):
        input_frame = ttk.LabelFrame(self.main_frame, text="输入信息", padding="10")
        input_frame.pack(fill=tk.X, pady=5)
        
        # 将input_frame保存为类属性
        self.input_frame = input_frame
        
        # 调整网格布局配置 - 所有列都不自动扩展
        input_frame.columnconfigure(1, weight=0)
        input_frame.columnconfigure(3, weight=0)
        input_frame.columnconfigure(5, weight=0)
        input_frame.columnconfigure(7, weight=0)
        input_frame.columnconfigure(9, weight=0)
        input_frame.columnconfigure(11, weight=0)
        input_frame.columnconfigure(13, weight=0)
        

        


        # 呼号
        ttk.Label(input_frame, text="呼号:").grid(row=1, column=0, padx=0, pady=2, sticky=tk.W)
        self.call_sign_var = tk.StringVar()
        self.call_sign_entry = ttk.Entry(input_frame, textvariable=self.call_sign_var, width=7)
        self.call_sign_entry.grid(row=1, column=1, padx=0, pady=2, sticky=tk.W)
        
        # 绑定呼号输入事件
        self.call_sign_var.trace_add("write", self.on_call_sign_change)
        
        # QTH - 城市选择功能
        qth_label_frame = ttk.Frame(input_frame)
        qth_label_frame.grid(row=1, column=2, padx=0, pady=2, sticky=tk.W)
        
        ttk.Label(qth_label_frame, text="QTH:").grid(row=0, column=0, padx=0, pady=0, sticky=tk.W)
        
        # 省份显示框（放在QTH标签后面）
        self.province_var = tk.StringVar()
        self.province_entry = ttk.Entry(qth_label_frame, textvariable=self.province_var, width=8, state='readonly')
        self.province_entry.grid(row=0, column=1, padx=(5, 0), pady=0, sticky=tk.W)
        
        # QTH输入框
        qth_frame = ttk.Frame(input_frame)
        qth_frame.grid(row=1, column=3, padx=0, pady=2, sticky=tk.W)
        qth_frame.columnconfigure(0, weight=0)
        qth_frame.columnconfigure(1, weight=0)
        
        self.qth_var = tk.StringVar()
        self.qth_entry = ttk.Entry(qth_frame, textvariable=self.qth_var, width=25)
        self.qth_entry.grid(row=0, column=0, sticky=tk.W)
        
        # 城市选择按钮
        self.city_select_btn = ttk.Button(qth_frame, text="选择城市", command=self.select_city)
        self.city_select_btn.grid(row=0, column=1, padx=2)
        
        # 绑定QTH输入事件
        self.qth_var.trace_add("write", self.on_qth_change)
        
        # 设备
        ttk.Label(input_frame, text="设备:").grid(row=1, column=6, padx=0, pady=2, sticky=tk.W)
        equipment_frame = ttk.Frame(input_frame)
        equipment_frame.grid(row=1, column=7, padx=0, pady=2, sticky=tk.W)
        equipment_frame.columnconfigure(0, weight=0)
        
        self.equipment_var = tk.StringVar()
        self.equipment_entry = ttk.Entry(equipment_frame, textvariable=self.equipment_var, width=15)
        self.equipment_entry.grid(row=0, column=0, sticky=tk.W)
        
        # 对讲机选择按钮
        self.walkie_talkie_btn = ttk.Button(equipment_frame, text="选择对讲机", command=self.select_walkie_talkie)
        self.walkie_talkie_btn.grid(row=0, column=1, padx=2)
        
        # 绑定设备输入事件
        self.equipment_var.trace_add("write", self.on_equipment_change)
        
        # 功率输入框 - 移到设备后面
        ttk.Label(input_frame, text="功率:").grid(row=1, column=8, padx=5, pady=2, sticky=tk.W)
        self.power_var = tk.StringVar()
        self.power_entry = ttk.Entry(input_frame, textvariable=self.power_var, width=10)
        self.power_entry.grid(row=1, column=9, padx=0, pady=2, sticky=tk.W)
        
        # 天馈参数输入框（移动到接入方式前面）
        ttk.Label(input_frame, text="天馈参数:").grid(row=1, column=10, padx=5, pady=2, sticky=tk.W)
        self.antenna_var = tk.StringVar()
        self.antenna_entry = ttk.Entry(input_frame, textvariable=self.antenna_var, width=10)
        self.antenna_entry.grid(row=1, column=11, padx=0, pady=2, sticky=tk.W)
        
        # 接入方式（输入框，支持数字转换，不显示提示文本）
        ttk.Label(input_frame, text="接入方式:").grid(row=1, column=12, padx=5, pady=2, sticky=tk.W)
        self.access_method_var = tk.StringVar()
        self.access_method_entry = ttk.Entry(
            input_frame, 
            textvariable=self.access_method_var,
            width=10
        )
        self.access_method_entry.grid(row=1, column=13, padx=5, pady=2, sticky=tk.W)
        
        # 绑定接入方式输入事件（用于数字转换）
        self.access_method_var.trace_add("write", self.on_access_method_change)
        
        # 绑定回车键导航和确认
        self.call_sign_entry.bind('<Return>', lambda event: self.qth_entry.focus())
        self.qth_entry.bind('<Return>', lambda event: self.equipment_entry.focus())
        self.equipment_entry.bind('<Return>', lambda event: self.power_entry.focus())
        # 动态回车键绑定函数
        def handle_return_key(event):
            network_type = self.network_type_var.get()
            if network_type == "模拟台网":
                # 模拟台网：天馈输入框回车添加记录
                self.add_record()
            else:
                # 数字台网：接入方式输入框回车添加记录
                self.add_record()
        
        # 功率输入框回车键绑定 - 根据网络类型动态跳转
        def handle_power_return(event):
            network_type = self.network_type_var.get()
            if network_type == "模拟台网":
                # 模拟台网：跳转到天馈输入框
                self.antenna_entry.focus()
            else:
                # 数字台网：跳过天馈，直接跳转到接入方式
                self.access_method_entry.focus()
        
        self.power_entry.bind('<Return>', handle_power_return)
        
        # 天馈参数输入框回车键绑定
        self.antenna_entry.bind('<Return>', lambda event: self.access_method_entry.focus())
        
        # 接入方式输入框回车键绑定
        self.access_method_entry.bind('<Return>', handle_return_key)
        
        # 在输入信息窗口下方添加接入方式列表
        ttk.Separator(input_frame, orient='horizontal').grid(row=2, column=0, columnspan=12, sticky='ew', pady=5)
        
        self.access_method_label = ttk.Label(
            input_frame, 
            text="接入方式列表: " + ("4-软件接入, 5-模拟中继, 6-模拟直频, 7-短波CW, 8-短波SSB, 9-短波FM" if hasattr(self, 'network_type_var') and self.network_type_var.get() == "模拟台网" else "1-热点盒子, 2-数字中继, 3-ROIP直通, 4-软件接入"), 
            font=("SimHei", 9),
            foreground="#555555"
        )
        self.access_method_label.grid(row=3, column=0, columnspan=14, padx=5, pady=5)
        
    def create_display_frame(self):
        display_frame = ttk.LabelFrame(self.main_frame, text="记录列表", padding="10")
        display_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # 创建表格，移除时隙列，添加确认时间列
        columns = ("名次", "呼号", "QTH", "设备", "功率", "天馈", "接入方式", "确认时间")
        self.tree = ttk.Treeview(display_frame, columns=columns, show="headings")
        
        # 设置列宽和标题
        column_widths = [60, 120, 150, 180, 80, 120, 120, 120]
        for i, col in enumerate(columns):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=column_widths[i], anchor=tk.CENTER)
        
        # 添加滚动条
        scrollbar_y = ttk.Scrollbar(display_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar_x = ttk.Scrollbar(display_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscroll=scrollbar_y.set, xscroll=scrollbar_x.set)
        
        # 布局
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar_y.grid(row=0, column=1, sticky=tk.NS)
        scrollbar_x.grid(row=1, column=0, sticky=tk.EW)
        
        display_frame.rowconfigure(0, weight=1)
        display_frame.columnconfigure(0, weight=1)
        
        # 绑定表格选择事件
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        
        # 为时间列应用放大字体
        self.tree.tag_configure('time_tag', font= ("SimHei", 12, "bold"))
        
        # 为主控行配置特殊样式
        self.tree.tag_configure('master_tag', font=("SimHei", 10, "bold"), background="#87CEEB")
        
        # 初始化主控行
        self.init_master_row()
        
    def init_master_row(self):
        """初始化主控行"""
        # 主控行不自动填充台网信息中的主控呼号
        # 插入空的主控行，序号为"主控"
        master_values = ("主控", "", "", "", "", "", "", datetime.now().strftime("%H:%M"))
        self.tree.insert("", 0, values=master_values, tags=('master_tag',))
    
    def update_master_row(self):
        """更新主控行"""
        # 先检查现有的主控行内容
        existing_master_call_sign = ""
        for child in self.tree.get_children():
            item = self.tree.item(child)
            if item['values'][0] == "主控":
                existing_master_call_sign = item['values'][1] if len(item['values']) > 1 else ""
                break
        
        # 先删除现有的主控行
        for child in self.tree.get_children():
            item = self.tree.item(child)
            if item['values'][0] == "主控":
                self.tree.delete(child)
                break
        
        # 重新插入主控行，保留现有的呼号内容
        if existing_master_call_sign and existing_master_call_sign.strip():
            master_values = ("主控", existing_master_call_sign, "", "", "", "", "", "")
        else:
            # 重新插入空的主控行，不自动填充台网信息中的主控呼号
            master_values = ("主控", "", "", "", "", "", "", datetime.now().strftime("%H:%M"))
        self.tree.insert("", 0, values=master_values, tags=('master_tag',))
    
    def update_master_row_with_operator_call_sign(self, operator_call_sign):
        """使用台网信息中的主控呼号更新主控行"""
        # 先删除现有的主控行
        for child in self.tree.get_children():
            item = self.tree.item(child)
            if item['values'][0] == "主控":
                self.tree.delete(child)
                break
        
        # 使用台网信息中的主控呼号重新插入主控行
        master_values = ("主控", operator_call_sign, "", "", "", "", "", datetime.now().strftime("%H:%M"))
        self.tree.insert("", 0, values=master_values, tags=('master_tag',))
        
    def create_button_frame(self):
        button_frame = ttk.Frame(self.main_frame, padding="10")
        button_frame.pack(fill=tk.X, pady=5)
        
        # 添加按钮
        self.add_button = ttk.Button(button_frame, text="添加记录", command=self.add_record)
        self.add_button.pack(side=tk.LEFT, padx=5)
        
        # 删除选中按钮
        self.delete_button = ttk.Button(button_frame, text="删除选中", command=self.delete_selected)
        self.delete_button.pack(side=tk.LEFT, padx=5)
        
        # 生成表单按钮
        self.export_button = ttk.Button(button_frame, text="生成表单", command=self.export_records)
        self.export_button.pack(side=tk.LEFT, padx=5)
        
        # 历史记录管理按钮
        self.history_button = ttk.Button(button_frame, text="历史记录管理", command=self.show_history)
        self.history_button.pack(side=tk.LEFT, padx=5)
        
        # 历史点名查询按钮
        self.search_button = ttk.Button(button_frame, text="历史点名查询", command=self.search_roll_call_records)
        self.search_button.pack(side=tk.LEFT, padx=5)
        
        # 清空历史按钮
        self.clear_history_button = ttk.Button(button_frame, text="清空历史", command=self.clear_history)
        self.clear_history_button.pack(side=tk.LEFT, padx=5)
        
        # 修改管理员密码按钮 - 默认隐藏，通过点击6次版权信息显示
        self.change_password_button = ttk.Button(button_frame, text="修改管理员密码", command=self.change_admin_password)
        # 抓取参数（最短通话时长/时间窗口）- 与管理员按钮一起，通过点击6次版权信息显示
        self.grab_settings_button = ttk.Button(button_frame, text="抓取参数", command=self.edit_grab_settings)
        # 初始化时不显示
        # self.change_password_button.pack(side=tk.LEFT, padx=5)
        
        # 结束按钮
        self.exit_button = ttk.Button(button_frame, text="结束", command=self.on_exit)
        self.exit_button.pack(side=tk.RIGHT, padx=5)
    
    def on_copyright_click(self, event):
        """处理版权信息点击事件"""
        self.copyright_click_count += 1
        
        # 点击6次时切换密码管理按钮的可见性
        if self.copyright_click_count == 6:
            try:
                # 检查按钮是否已显示，使用try-except捕获可能的异常
                is_packed = self.change_password_button.pack_info()
                
                if is_packed:
                    # 按钮已显示，隐藏它们
                    self.change_password_button.pack_forget()
                    self.grab_settings_button.pack_forget()
                else:
                    # 按钮未显示，显示它们
                    self.change_password_button.pack(side=tk.LEFT, padx=5)
                    self.grab_settings_button.pack(side=tk.LEFT, padx=5)
            except tk.TclError:
                # 捕获TclError异常，表示按钮未被pack
                # 按钮未显示，显示它们
                self.change_password_button.pack(side=tk.LEFT, padx=5)
                self.grab_settings_button.pack(side=tk.LEFT, padx=5)
                
            # 重置计数器
            self.copyright_click_count = 0
    
    def change_admin_password(self):
        """修改管理员密码"""
        # 确保可以访问tk模块
        import tkinter as tk
        from tkinter import ttk, messagebox
        import sqlite3
        
        # 创建修改密码对话框
        password_window = tk.Toplevel(self.root)
        password_window.title("修改管理员密码")
        password_window.geometry("400x300")
        password_window.transient(self.root)
        password_window.grab_set()
        
        # 创建输入框架
        input_frame = ttk.Frame(password_window, padding="20")
        input_frame.pack(fill=tk.BOTH, expand=True)
        
        # 旧密码输入
        ttk.Label(input_frame, text="当前密码:", font=("SimHei", 10)).grid(row=0, column=0, padx=10, pady=15, sticky=tk.E)
        old_password_var = tk.StringVar()
        old_password_entry = ttk.Entry(input_frame, textvariable=old_password_var, show="*", width=20)
        old_password_entry.grid(row=0, column=1, padx=10, pady=15, sticky=tk.W)
        
        # 新密码输入
        ttk.Label(input_frame, text="新密码:", font=("SimHei", 10)).grid(row=1, column=0, padx=10, pady=15, sticky=tk.E)
        new_password_var = tk.StringVar()
        new_password_entry = ttk.Entry(input_frame, textvariable=new_password_var, show="*", width=20)
        new_password_entry.grid(row=1, column=1, padx=10, pady=15, sticky=tk.W)
        
        # 确认新密码输入
        ttk.Label(input_frame, text="确认新密码:", font=("SimHei", 10)).grid(row=2, column=0, padx=10, pady=15, sticky=tk.E)
        confirm_password_var = tk.StringVar()
        confirm_password_entry = ttk.Entry(input_frame, textvariable=confirm_password_var, show="*", width=20)
        confirm_password_entry.grid(row=2, column=1, padx=10, pady=15, sticky=tk.W)
        
        # 默认密码提示
        ttk.Label(input_frame, text="默认密码为 BH2VLF电话 15641966099", font=("SimHei", 9), foreground="gray").grid(row=3, column=0, columnspan=2, padx=10, pady=(0, 5))
        
        # 处理密码修改
        def handle_change_password():
            old_password = old_password_var.get()
            new_password = new_password_var.get()
            confirm_password = confirm_password_var.get()
            
            # 验证输入
            if not old_password or not new_password or not confirm_password:
                messagebox.showerror("输入错误", "所有密码字段都不能为空！")
                return
            
            if new_password != confirm_password:
                messagebox.showerror("输入错误", "两次输入的新密码不一致！")
                return
            
            # 验证旧密码
            try:
                # 首先尝试获取password_hash列
                try:
                    self.cursor.execute("SELECT password_hash FROM admin ORDER BY id DESC LIMIT 1")
                    result = self.cursor.fetchone()
                    if result and result[0]:
                        # 计算输入旧密码的哈希值并与数据库中的进行比较
                        old_password_hash = hashlib.sha256(old_password.encode()).hexdigest()
                        if old_password_hash != result[0]:
                            messagebox.showerror("密码错误", "当前密码不正确！")
                            return
                    else:
                        # 如果password_hash为空或不存在，尝试使用password列（向后兼容）
                        self.cursor.execute("SELECT password FROM admin ORDER BY id DESC LIMIT 1")
                        result = self.cursor.fetchone()
                        if not result or old_password != result[0]:
                            messagebox.showerror("密码错误", "当前密码不正确！")
                            return
                except sqlite3.OperationalError:
                    # 如果password_hash列不存在，尝试使用password列（向后兼容）
                    self.cursor.execute("SELECT password FROM admin ORDER BY id DESC LIMIT 1")
                    result = self.cursor.fetchone()
                    if not result or old_password != result[0]:
                        messagebox.showerror("密码错误", "当前密码不正确！")
                        return
            except sqlite3.Error as e:
                messagebox.showerror("数据库错误", f"验证密码失败: {str(e)}")
                return
            
            # 更新密码（存储哈希值）
            try:
                new_password_hash = hashlib.sha256(new_password.encode()).hexdigest()
                
                # 先检查表结构，了解哪些列可用
                self.cursor.execute("PRAGMA table_info(admin)")
                columns = [column[1] for column in self.cursor.fetchall()]
                
                # 优先使用更新现有记录的方式
                try:
                    # 检查是否有现有记录
                    self.cursor.execute("SELECT COUNT(*) FROM admin")
                    count = self.cursor.fetchone()[0]
                    
                    if count > 0:
                        # 有现有记录，直接更新
                        update_columns = []
                        update_params = []
                        
                        # 总是尝试更新password_hash列
                        if 'password_hash' in columns:
                            update_columns.append("password_hash = ?")
                            update_params.append(new_password_hash)
                        
                        # 如果存在password列，不设置为NULL，避免可能的NOT NULL约束问题
                        # 而是直接跳过，不更新该列
                        
                        if update_columns:
                            update_sql = f"UPDATE admin SET {', '.join(update_columns)} WHERE id = (SELECT MAX(id) FROM admin)"
                            self.cursor.execute(update_sql, update_params)
                        else:
                            # 如果没有可用的列，尝试插入新记录
                            raise Exception("No suitable columns available for update")
                    else:
                        # 没有现有记录，插入新记录
                        # 根据可用列决定插入哪些值
                        if 'password_hash' in columns:
                            # 只插入password_hash列，避免password列的NOT NULL约束问题
                            self.cursor.execute("INSERT INTO admin (password_hash) VALUES (?)", (new_password_hash,))
                        elif 'password' in columns:
                            # 如果只有password列（向后兼容）
                            self.cursor.execute("INSERT INTO admin (password) VALUES (?)", (new_password,))
                        else:
                            # 如果没有合适的列，创建一条默认记录
                            self.cursor.execute("INSERT INTO admin DEFAULT VALUES")
                            last_id = self.cursor.lastrowid
                            
                            # 然后尝试添加password_hash列并更新
                            if 'password_hash' not in columns:
                                try:
                                    self.cursor.execute("ALTER TABLE admin ADD COLUMN password_hash TEXT")
                                except:
                                    pass  # 忽略列已存在的错误
                            
                            # 再次尝试更新
                            self.cursor.execute("UPDATE admin SET password_hash = ? WHERE id = ?", (new_password_hash, last_id))
                except Exception:
                    # 作为最后的后备方案，使用简单的插入方式
                    # 检查是否可以直接插入仅包含password_hash的记录
                    try:
                        self.cursor.execute("INSERT INTO admin (password_hash) VALUES (?)", (new_password_hash,))
                    except:
                        # 如果仍然失败，尝试插入空记录然后更新
                        self.cursor.execute("INSERT INTO admin DEFAULT VALUES")
                        last_id = self.cursor.lastrowid
                        
                        # 使用COALESCE避免NULL冲突
                        update_sql = """
                        UPDATE admin SET 
                            password_hash = ?
                        WHERE id = ?
                        """
                        self.cursor.execute(update_sql, (new_password_hash, last_id))
                
                self.conn.commit()
                messagebox.showinfo("成功", "管理员密码修改成功！")
                password_window.destroy()
            except sqlite3.Error as e:
                messagebox.showerror("数据库错误", f"更新密码失败: {str(e)}")
        
        # 创建按钮框架
        button_frame = ttk.Frame(password_window, padding="10")
        button_frame.pack(fill=tk.X)
        
        # 确认按钮
        confirm_button = ttk.Button(button_frame, text="确认修改", command=handle_change_password)
        confirm_button.pack(side=tk.LEFT, padx=5, pady=5)
        
        # 取消按钮
        cancel_button = ttk.Button(button_frame, text="取消", command=password_window.destroy)
        cancel_button.pack(side=tk.LEFT, padx=5, pady=5)
    
    def create_digital_network_frame(self):
        """创建抓取盒子功能区域"""
        # 创建数字台网框架
        self.digital_network_frame = ttk.LabelFrame(self.main_frame, text="抓取盒子", padding="5")
        
        # 初始化时不显示抓取盒子，由network_type_var的变化来控制显示/隐藏
        # self.digital_network_frame.pack(fill=tk.X, pady=5)
        
        # 手动触发一次台网类型变化事件，确保根据当前选择正确显示/隐藏抓取盒子
        self.on_network_type_change()
        
        # 创建pi-star测试工具实例 - 初始化为None，稍后在连接测试时创建
        self.pistar_tester = None
        
        # 创建输入框架
        input_frame = ttk.Frame(self.digital_network_frame)
        input_frame.pack(fill=tk.X, pady=5)
        
        # 移除自适应列宽配置，使用固定宽度布局
        
        # 添加5个水平排列的空白备选框
        self.alternative_entries = []
        # 将备选框移到盒子地址前面
        ttk.Label(input_frame, text="备选框:").grid(row=0, column=0, sticky=tk.W, pady=2, padx=3)
        for i in range(5):
            entry = ttk.Entry(input_frame, width=10)
            entry.grid(row=0, column=i+1, padx=(0, 0), pady=2, sticky=tk.W)
            # 添加点击事件绑定，点击备选框时将呼号填充到主呼号输入框
            entry.bind('<Button-1>', lambda event, idx=i: self.on_alternative_entry_click(idx))
            self.alternative_entries.append(entry)
        
        # IP地址/主机名输入（默认显示上次成功连接的盒子地址）
        ttk.Label(input_frame, text="盒子地址:").grid(row=0, column=7, padx=3, pady=2, sticky=tk.W)
        self.pistar_host_var = tk.StringVar(value=self._load_last_box_host())
        self.pistar_host_entry = ttk.Entry(input_frame, textvariable=self.pistar_host_var, width=20)
        self.pistar_host_entry.grid(row=0, column=8, padx=3, pady=2, sticky=tk.W)
        # 双击地址框直接用浏览器打开盒子网页
        self.pistar_host_entry.bind('<Double-Button-1>', self._pistar_open_box_web)
        
        # 创建用户名和密码的高级选项框架（默认隐藏）
        self.advanced_options_frame = ttk.Frame(input_frame)
        self.advanced_options_frame.grid(row=1, column=0, columnspan=14, padx=5, pady=5, sticky=tk.W)
        
        # 用户名输入 - 在高级选项框架中
        ttk.Label(self.advanced_options_frame, text="用户名:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.pistar_username_var = tk.StringVar(value="pi-star")
        self.pistar_username_entry = ttk.Entry(self.advanced_options_frame, textvariable=self.pistar_username_var, width=15)
        self.pistar_username_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)
        
        # 密码输入 - 在高级选项框架中
        ttk.Label(self.advanced_options_frame, text="密码:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        self.pistar_password_var = tk.StringVar(value="raspberry")
        self.pistar_password_entry = ttk.Entry(self.advanced_options_frame, textvariable=self.pistar_password_var, show="*", width=15)
        self.pistar_password_entry.grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)
        
        # 初始隐藏高级选项
        self.advanced_options_frame.grid_remove()
        
        # 用于显示高级选项的按钮
        self.show_advanced_button = ttk.Button(input_frame, text="显示高级选项", command=self.toggle_advanced_options)
        self.show_advanced_button.grid(row=0, column=9, padx=3, pady=2, sticky=tk.W)
        # 默认隐藏高级选项按钮
        self.show_advanced_button.grid_remove()
        
        # 在输入框架中添加所有按钮，使其与主机地址在同一行显示
        # “搜索”与“连接”分开：可手填地址直接连接，也可先搜索出地址再连接
        self.box_action_frame = ttk.Frame(input_frame)
        self.box_action_frame.grid(row=0, column=10, padx=3, pady=2, sticky=tk.W)
        self.pistar_search_button = ttk.Button(
            self.box_action_frame, text="搜索", width=6, command=self._pistar_search_box)
        self.pistar_search_button.pack(side=tk.LEFT)
        self.connection_button = ttk.Button(
            self.box_action_frame, text="连接", width=6, command=self.pistar_toggle_connection)
        self.connection_button.pack(side=tk.LEFT, padx=(4, 0))

        # 时隙选择单选框
        self.timeslot_var = tk.IntVar(value=1)  # 默认选择时隙1
        ttk.Radiobutton(input_frame, text="时隙1", variable=self.timeslot_var, value=1).grid(row=0, column=11, padx=(3, 0), pady=1, sticky=tk.W)
        ttk.Radiobutton(input_frame, text="时隙2", variable=self.timeslot_var, value=2).grid(row=0, column=11, padx=(60, 3), pady=1, sticky=tk.W)

        # 抓取呼号按钮
        self.pistar_test_button = ttk.Button(input_frame, text="抓取呼号", command=self.pistar_run_test, state='disabled')
        self.pistar_test_button.grid(row=0, column=12, padx=3, pady=2, sticky=tk.W)
        
        # 创建日志区域框架
        self.log_frame = ttk.LabelFrame(self.digital_network_frame, text="测试日志")
        self.log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        # 默认隐藏日志区域
        self.log_frame.pack_forget()
        
        # 创建文本框用于显示日志
        self.pistar_log_text = tk.Text(self.log_frame, height=8, width=80, font= ("SimHei", 9))
        self.pistar_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 添加滚动条
        log_scrollbar = ttk.Scrollbar(self.log_frame, command=self.pistar_log_text.yview)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)
        self.pistar_log_text.config(yscrollcommand=log_scrollbar.set)
        
        # 通话记录列表框架
        self.records_frame = ttk.LabelFrame(self.digital_network_frame, text="通话记录")
        self.records_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        # 默认隐藏通话记录区域
        self.records_frame.pack_forget()
        
        # 创建表格显示通话记录
        columns = ("时间", "呼号", "时隙", "误码率")
        self.call_records_tree = ttk.Treeview(self.records_frame, columns=columns, show="headings")

        # 设置列宽和标题
        column_widths = [120, 100, 60, 80]
        for i, col in enumerate(columns):
            self.call_records_tree.heading(col, text=col)
            self.call_records_tree.column(col, width=column_widths[i], anchor=tk.CENTER)

        # 添加滚动条
        records_scrollbar_y = ttk.Scrollbar(self.records_frame, orient=tk.VERTICAL, command=self.call_records_tree.yview)
        records_scrollbar_x = ttk.Scrollbar(self.records_frame, orient=tk.HORIZONTAL, command=self.call_records_tree.xview)
        self.call_records_tree.configure(yscroll=records_scrollbar_y.set, xscroll=records_scrollbar_x.set)

        # 布局
        self.call_records_tree.grid(row=0, column=0, sticky=tk.NSEW)
        records_scrollbar_y.grid(row=0, column=1, sticky=tk.NS)
        records_scrollbar_x.grid(row=1, column=0, sticky=tk.EW)

        self.records_frame.rowconfigure(0, weight=1)
        self.records_frame.columnconfigure(0, weight=1)
        
    def toggle_advanced_options(self):
        """切换高级选项的显示状态"""
        if self.advanced_options_frame.winfo_manager() == 'grid':
            self.advanced_options_frame.grid_remove()
            self.show_advanced_button.config(text="显示高级选项")
        else:
            self.advanced_options_frame.grid()
            self.show_advanced_button.config(text="隐藏高级选项")
        
    def pistar_log_message(self, message):
        """在界面上显示日志消息"""
        # 在主线程中更新UI
        self.root.after(0, lambda: self._pistar_log_message_async(message))
        
    def _pistar_log_message_async(self, message):
        """异步更新日志UI（但不显示日志窗口）"""
        # 仅记录日志到文本控件，但不显示日志窗口
        self.pistar_log_text.insert(tk.END, message + "\n")
        self.pistar_log_text.see(tk.END)
        
    # 原始的pistar_connect_test方法已被重命名为_pistar_connect并集成到pistar_toggle_connection中
        
    @staticmethod
    def _load_last_box_host():
        """从 app_config.ini 读取上次成功连接的盒子地址，无记录时返回空串"""
        try:
            config = configparser.ConfigParser()
            config_file = "app_config.ini"
            if os.path.exists(config_file):
                config.read(config_file, encoding='utf-8')
                if config.has_section("GrabBox") and config.has_option("GrabBox", "last_host"):
                    return config.get("GrabBox", "last_host").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _save_last_box_host(host):
        """把成功连接的盒子地址写入 app_config.ini，作为下次启动的默认值"""
        try:
            config = configparser.ConfigParser()
            config_file = "app_config.ini"
            if os.path.exists(config_file):
                config.read(config_file, encoding='utf-8')
            if not config.has_section("GrabBox"):
                config.add_section("GrabBox")
            config.set("GrabBox", "last_host", host)
            with open(config_file, "w", encoding='utf-8') as f:
                config.write(f)
        except Exception:
            pass

    @staticmethod
    def _grab_config_path():
        """抓取参数所在配置文件（exe/脚本同目录的 app_config.ini）"""
        return os.path.join(APP_DIR, "app_config.ini")

    def _load_grab_settings(self):
        """读取 [GrabBox] 中的最短通话时长/最近通话窗口，无记录时用默认值 3 秒 / 3 分钟"""
        try:
            config = configparser.ConfigParser()
            path = self._grab_config_path()
            if os.path.exists(path):
                config.read(path, encoding='utf-8')
                if config.has_section("GrabBox"):
                    self.grab_min_duration = max(0, config.getint("GrabBox", "min_duration", fallback=3))
                    self.grab_window_minutes = max(1, config.getint("GrabBox", "window_minutes", fallback=3))
        except Exception:
            pass
        self._apply_grab_settings()

    def _apply_grab_settings(self):
        """把最短通话时长写入抓取模块（模块级变量，解析记录时实时生效）"""
        pi_star_http_test.MIN_CALL_DURATION = max(0, int(self.grab_min_duration))

    def _save_grab_settings(self):
        """把抓取参数写入 app_config.ini 的 [GrabBox] 段"""
        try:
            config = configparser.ConfigParser()
            path = self._grab_config_path()
            if os.path.exists(path):
                config.read(path, encoding='utf-8')
            if not config.has_section("GrabBox"):
                config.add_section("GrabBox")
            config.set("GrabBox", "min_duration", str(self.grab_min_duration))
            config.set("GrabBox", "window_minutes", str(self.grab_window_minutes))
            with open(path, "w", encoding='utf-8') as f:
                config.write(f)
        except Exception as e:
            print(f"保存抓取参数失败: {e}")

    def edit_grab_settings(self):
        """主控修改抓取参数：最短通话时长（秒）与最近通话窗口（分钟），数字输入"""
        import tkinter as tk
        from tkinter import ttk, messagebox

        win = tk.Toplevel(self.root)
        win.title("抓取参数设置")
        win.geometry("380x240")
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="最短通话时长（秒）:").grid(row=0, column=0, sticky=tk.W, pady=8)
        min_duration_var = tk.StringVar(value=str(self.grab_min_duration))
        ttk.Spinbox(frame, from_=0, to=60, textvariable=min_duration_var, width=10).grid(
            row=0, column=1, sticky=tk.W, pady=8)

        ttk.Label(frame, text="最近通话窗口（分钟）:").grid(row=1, column=0, sticky=tk.W, pady=8)
        window_minutes_var = tk.StringVar(value=str(self.grab_window_minutes))
        ttk.Spinbox(frame, from_=1, to=1440, textvariable=window_minutes_var, width=10).grid(
            row=1, column=1, sticky=tk.W, pady=8)

        ttk.Label(
            frame,
            text="说明：通话时长低于设定值的记录不抓取（正在通话的最新一条除外）；时间窗口决定哪些呼叫进入备选框。",
            foreground="#666666", wraplength=330, justify=tk.LEFT
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        def save():
            try:
                min_duration = int(min_duration_var.get().strip())
                window_minutes = int(window_minutes_var.get().strip())
            except ValueError:
                messagebox.showerror("错误", "请输入整数", parent=win)
                return
            if min_duration < 0 or window_minutes < 1:
                messagebox.showerror("错误", "最短通话时长需 ≥ 0 秒，时间窗口需 ≥ 1 分钟", parent=win)
                return
            self.grab_min_duration = min_duration
            self.grab_window_minutes = window_minutes
            self._apply_grab_settings()
            self._save_grab_settings()
            messagebox.showinfo(
                "完成",
                f"已保存：最短通话时长 {min_duration} 秒，最近通话窗口 {window_minutes} 分钟",
                parent=win)
            win.destroy()

        ttk.Button(frame, text="保存", command=save).grid(row=3, column=0, pady=15, sticky=tk.W)
        ttk.Button(frame, text="取消", command=win.destroy).grid(row=3, column=1, pady=15, sticky=tk.W)

    def _pistar_connect_result(self, success, error_message=None):
        """处理连接测试结果"""
        if success:
            # 显示识别到的盒子系统类型
            sys_type = getattr(self.pistar_tester, 'system_type', 'unknown') if self.pistar_tester else 'unknown'
            sys_name = pi_star_http_test.SYSTEM_DISPLAY_NAMES.get(sys_type, sys_type)
            messagebox.showinfo("成功", f"盒子连接测试成功\n系统类型: {sys_name}")
            # 保存本次成功连接的盒子地址，作为下次启动的默认值
            self._save_last_box_host(self.pistar_host_var.get().strip())
            # 更新按钮状态（已连接时禁止再搜索）
            self.connection_button.config(text="断开", state='normal')
            self.pistar_search_button.config(state='disabled')
            self.pistar_test_button.config(state='normal')
        else:
            error_text = "盒子连接测试失败"
            if error_message:
                error_text += f": {error_message}"
            messagebox.showerror("错误", error_text)
            # 重置测试器
            self.pistar_tester = None
            # 重新启用连接与搜索按钮
            self.connection_button.config(text="连接", state='normal')
            self.pistar_search_button.config(state='normal')
            # 如果是验证错误，自动显示高级选项按钮和框架
            if error_message and ("认证" in error_message or "登录" in error_message or "password" in error_message.lower() or "username" in error_message.lower()):
                # 显示高级选项按钮
                self.show_advanced_button.grid()
                # 显示高级选项框架
                if self.advanced_options_frame.winfo_manager() != 'grid':
                    self.toggle_advanced_options()
        
    def _pistar_open_box_web(self, event=None):
        """双击地址框：用系统默认浏览器打开盒子网页"""
        host = self.pistar_host_var.get().strip()
        if not host:
            messagebox.showinfo("提示", "地址为空，请先输入盒子 IP 或主机名")
            return "break"
        url = host if host.lower().startswith(("http://", "https://")) else "http://" + host
        try:
            webbrowser.open(url)
            self.pistar_log_message(f"已在浏览器打开: {url}")
        except Exception as exc:
            messagebox.showerror("错误", f"无法打开浏览器: {exc}")
        return "break"

    def _pistar_search_box(self):
        """启动局域网盒子搜索（搜索出地址后由用户点“连接”）"""
        if self.pistar_tester is not None:
            messagebox.showinfo("提示", "请先断开当前连接再搜索")
            return
        self.pistar_search_button.config(text="搜索中...", state='disabled')
        self.connection_button.config(state='disabled')
        self.pistar_host_entry.config(state='disabled')
        self.pistar_log_message("开始在局域网内搜索盒子...")

        def search_task():
            found_ip = self._do_lan_search()
            self.root.after(0, lambda: self._pistar_search_result(found_ip))

        threading.Thread(target=search_task, daemon=True).start()

    @staticmethod
    def _detect_local_ip():
        """获取本机默认路由的局域网IP"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    @staticmethod
    def _local_subnets():
        """枚举本机所有可用网卡的 /24 网段前缀（排除回环和 169.254 自动地址）"""
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        default_ip = RollCallApp._detect_local_ip()
        if not default_ip.startswith("127."):
            ips.add(default_ip)
        prefixes = set()
        for ip in ips:
            if ip.startswith(("127.", "169.254.")):
                continue
            prefixes.add(ip.rsplit(".", 1)[0] + ".")
        return prefixes

    @staticmethod
    def _is_port_open(ip, port=80, timeout=0.6):
        """快速判断端口是否开放（比直接发 HTTP 请求快得多）"""
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _pistar_probe_host(self, ip):
        """探测指定IP是否为 Pi-Star / WPSD / J-Star 盒子；是则返回系统类型，否则返回 None"""
        try:
            # 连接1.5秒快速排除无设备地址；读取放宽到6秒（盒子负载高时页面生成可能超过4秒）
            resp = requests.get(f"http://{ip}", timeout=(1.5, 6), allow_redirects=True)
            # WWW-Authenticate（401 认证域）和页面内容都参与识别，兼容 J-Star 等改版固件
            sample = (resp.headers.get("WWW-Authenticate", "") + " " + resp.text[:4096]).lower()
            for token in ("pi-star", "pistar", "wpsd", "j-star", "jstar", "bi7jta"):
                if token in sample:
                    detected = pi_star_http_test.PiStarHttpTester.detect_system_type(sample)
                    return detected or pi_star_http_test.SYSTEM_UNKNOWN
        except Exception:
            pass
        return None

    def _do_lan_search(self):
        """执行局域网搜索：先试常见主机名，再并发扫描本机所有 /24 网段"""
        # 1. 先试 Pi-Star/WPSD 常见主机名
        for name in ("pi-star", "pi-star.local", "wpsd", "wpsd.local"):
            try:
                ip = socket.gethostbyname(name)
                if ip and not ip.startswith("127.") and self._pistar_probe_host(ip):
                    self.pistar_log_message(f"通过主机名 {name} 发现盒子: {ip}")
                    return ip
            except OSError:
                continue

        # 2. 扫描本机所有可用网段（兼容多网卡/无外网的情况）
        prefixes = self._local_subnets()
        if prefixes:
            found = self._scan_networks(prefixes)
            if found:
                return found
        else:
            self.pistar_log_message("未检测到常规局域网网段，请检查网线/WiFi 连接")
        # 3. 兜底：直连盒子时常用的默认网段（Pi-Star/WPSD 出厂默认 192.168.50.1）
        if "192.168.50." not in prefixes:
            self.pistar_log_message("继续尝试盒子默认网段 192.168.50.0/24 ...")
            return self._scan_networks({"192.168.50."})
        return None

    def _scan_networks(self, prefixes):
        """扫描给定 /24 网段前缀列表，返回第一个识别到的盒子 IP"""
        local_ips = {self._detect_local_ip()}
        candidates = []
        for prefix in sorted(prefixes):
            candidates += [prefix + str(i) for i in range(1, 255) if prefix + str(i) not in local_ips]
        self.pistar_log_message(f"开始扫描网段: {', '.join(sorted(prefixes))}0/24（共 {len(candidates)} 个地址）")

        # 2.1 先快速探测 80 端口，只对开放的主机发 HTTP 请求
        open_hosts = []
        with ThreadPoolExecutor(max_workers=128) as pool:
            futures = {pool.submit(self._is_port_open, ip): ip for ip in candidates}
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        open_hosts.append(futures[fut])
                except Exception:
                    pass
        if not open_hosts:
            self.pistar_log_message("网段内没有开放 80 端口的主机")
            return None
        self.pistar_log_message(f"发现 {len(open_hosts)} 个开放 80 端口的主机，正在识别盒子类型...")

        # 2.2 对开放主机做 HTTP 识别
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = {pool.submit(self._pistar_probe_host, ip): ip for ip in open_hosts}
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        for f in futures:
                            f.cancel()
                        return futures[fut]
                except Exception:
                    pass
        return None

    def _pistar_search_result(self, found_ip):
        """处理搜索结果"""
        self.pistar_search_button.config(text="搜索", state='normal')
        self.pistar_host_entry.config(state='normal')
        if found_ip:
            self.pistar_host_var.set(found_ip)
            self.connection_button.config(text="连接", state='normal')
            self.pistar_log_message(f"发现盒子: {found_ip}，请点击“连接”")
        else:
            self.connection_button.config(text="连接", state='normal')
            self.pistar_log_message("未在局域网内发现盒子")
            messagebox.showinfo("搜索结果", "未在局域网内发现盒子，请确认盒子已开机并接入同一网络，或手动输入盒子地址")

    def pistar_toggle_connection(self):
        """切换连接状态 - 连接或断开连接"""
        # 判断当前连接状态
        if self.pistar_tester is None:
            # 当前未连接，执行连接操作
            self._pistar_connect()
        else:
            # 当前已连接，执行断开操作
            self._pistar_disconnect()
            
    def _pistar_connect(self):
        """执行连接操作"""
        host = self.pistar_host_var.get().strip()
        username = self.pistar_username_var.get().strip()
        password = self.pistar_password_var.get().strip()
        
        if not host:
            messagebox.showerror("错误", "请输入盒子地址")
            return
        
        # 更新测试器配置
        self.pistar_tester = pi_star_http_test.PiStarHttpTester(host, username, password)
        self.pistar_tester.set_log_callback(self.pistar_log_message)
        # 设置时隙选择（timeslot_var 是GUI类的属性，不需要赋值给测试器）
        
        # 更新按钮文本（连接过程中禁用搜索）
        self.connection_button.config(text="连接中...", state='disabled')
        self.pistar_search_button.config(state='disabled')
        
        # 在新线程中执行连接测试
        def connect_task():
            try:
                if self.pistar_tester is not None:
                    result = self.pistar_tester.test_connection()
                    self.root.after(0, lambda: self._pistar_connect_result(result))
                else:
                    self.root.after(0, lambda: self._pistar_connect_result(False, "测试器未初始化"))
            except Exception as e:
                self.root.after(0, lambda e=e: self._pistar_connect_result(False, str(e)))
        
        # 启动连接线程
        threading.Thread(target=connect_task, daemon=True).start()
        
    def _pistar_disconnect(self):
        """断开连接"""
        # 重置测试器
        self.pistar_tester = None
        
        # 更新UI状态
        self.connection_button.config(text="连接", state='normal')
        self.pistar_search_button.config(text="搜索", state='normal')
        self.pistar_test_button.config(state='disabled')
        
        # 清空通话记录
        for item in self.call_records_tree.get_children():
            self.call_records_tree.delete(item)
        
        messagebox.showinfo("信息", "已断开与盒子的连接")
        
    def pistar_run_test(self):
        """运行单次测试"""
        # 禁用按钮防止重复点击
        self.pistar_test_button.config(state='disabled')
        
        # 在新线程中执行测试
        def test_task():
            try:
                # 执行登录
                login_success = self.pistar_tester.login()
                if not login_success:
                    self.root.after(0, lambda: messagebox.showerror("错误", "登录失败"))
                    # 自动显示高级选项以便用户修改用户名和密码
                    self.root.after(0, lambda: self.show_advanced_button.grid())
                    self.root.after(0, lambda: self.toggle_advanced_options() if self.advanced_options_frame.winfo_manager() != 'grid' else None)
                    return
                
                # 获取状态信息
                status_info = self.pistar_tester.get_status_info()
                print(f"pistar_run_test: 获取到status_info: {status_info is not None}")
                if status_info:
                    print(f"pistar_run_test: status_info中的通话记录: {'call_records' in status_info and len(status_info['call_records']) or 0}条")
                    # 更新通话记录
                    self.root.after(0, lambda: self._update_call_records(status_info))
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("错误", f"测试失败: {str(e)}"))
            finally:
                # 重新启用按钮
                self.root.after(0, lambda: self.pistar_test_button.config(state='normal'))
        
        # 启动测试线程
        threading.Thread(target=test_task, daemon=True).start()
        
    def on_alternative_entry_click(self, index):
        """当点击备选框时，将呼号填充到主呼号输入框"""
        if 0 <= index < len(self.alternative_entries):
            call_sign = self.alternative_entries[index].get().strip()
            if call_sign:
                self.call_sign_var.set(call_sign)
                # 聚焦到主呼号输入框
                if hasattr(self, 'call_sign_entry'):
                    self.call_sign_entry.focus()
                    # 选中输入框中的文本，方便用户修改
                    self.call_sign_entry.selection_range(0, tk.END)

    def _update_call_records(self, status_info):
        """更新通话记录表格（但不显示通话记录窗口）"""
        try:
            # 清空现有记录
            for item in self.call_records_tree.get_children():
                self.call_records_tree.delete(item)

            # 添加新记录
            call_signs_with_time = []
            if 'call_records' in status_info and status_info['call_records']:
                print(f"通话记录数量: {len(status_info['call_records'])} ")
                
                # 获取当前时间和时间窗口起点（窗口时长可由主控在“抓取参数”中修改）
                current_time = datetime.now()
                window_start = current_time - timedelta(minutes=self.grab_window_minutes)
                print(f"当前时间: {current_time}, 时间窗口起点: {window_start}（{self.grab_window_minutes} 分钟）")
                
                # 解析时间字符串的函数
                def parse_time_str(time_str):
                    try:
                        # 12小时制（如 "09:43:39 PM Nov 9"）统一交给抓取模块解析
                        if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*(AM|PM)', time_str or '', re.IGNORECASE):
                            return pi_star_http_test.PiStarHttpTester.parse_time_text(time_str, current_time)
                        # 处理格式如 "HH:MM:SS Oct 2nd" 的时间
                        if ' ' in time_str and any(month in time_str for month in ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']):
                            parts = time_str.split()
                            time_part = parts[0]  # HH:MM:SS
                            month_part = parts[1]  # Oct
                            day_part = parts[2]    # 2nd, 3rd, etc.
                            
                            # 解析日（去掉序数词后缀）
                            day = int(re.search(r'\d+', day_part).group())
                            
                            # 解析月
                            month_map = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                                         'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
                            month = month_map[month_part]
                            
                            # 解析时间
                            hour, minute, second = map(int, time_part.split(':'))
                            
                            # 使用当前年份（页面时间可能是UTC，按启发式修正为本地时间）
                            return pi_star_http_test.PiStarHttpTester.fix_tz_offset(
                                datetime(current_time.year, month, day, hour, minute, second), current_time)
                        # 处理其他可能的格式
                        elif ':' in time_str:
                            time_parts = time_str.split(':')
                            if len(time_parts) == 3:
                                hour, minute, second = map(int, time_parts)
                                return pi_star_http_test.PiStarHttpTester.fix_tz_offset(
                                    datetime(current_time.year, current_time.month, current_time.day, hour, minute, second), current_time)
                            elif len(time_parts) == 2:
                                hour, minute = map(int, time_parts)
                                return pi_star_http_test.PiStarHttpTester.fix_tz_offset(
                                    datetime(current_time.year, current_time.month, current_time.day, hour, minute, 0), current_time)
                    except Exception as e:
                        print(f"解析时间字符串 '{time_str}' 出错: {str(e)}")
                    return None
                
                # 存储所有通话记录，并标记哪些是RF来源的
                all_records = []
                rf_records = []
                recent_net_records = []
                
                for record in status_info['call_records']:
                    # 提取所需信息
                    time_str = record.get('time', '')
                    call_sign = record.get('call_sign', '')
                    slot = record.get('slot', '')
                    ber = record.get('ber', '')
                    source = record.get('source', '').lower()  # 获取来源并转为小写便于比较
                    
                    # 解析时间
                    record_time = parse_time_str(time_str)
                    
                    print(f"记录: 呼号={call_sign}, 时隙={slot}, 时间={time_str}, 来源={source}, 解析后时间={record_time}")

                    # 添加到表格
                    self.call_records_tree.insert('', tk.END, values=(time_str, call_sign, slot, ber))
                    
                    # 存储记录信息
                    all_records.append((time_str, call_sign, source))
                    
                    # 收集所有RF来源的记录
                    if source == 'rf' and call_sign:
                        rf_records.append((time_str, call_sign))
                        print(f"找到RF来源记录: {call_sign} (时间: {time_str})")
                    
                    # 收集时间窗口内的net呼叫
                    if source == 'net' and call_sign and record_time and record_time >= window_start:
                        print(f"收集窗口内的net呼叫: {call_sign} (时间: {time_str}, 解析后时间: {record_time})")
                        recent_net_records.append((record_time, call_sign))
                
                # 如果有时间窗口内的net呼叫
                if recent_net_records:
                    # 按时间倒序排序（最新的在前）
                    recent_net_records.sort(reverse=True, key=lambda x: x[0])
                    
                    # 提取呼号
                    call_signs_with_time = [(str(record_time), call_sign) for record_time, call_sign in recent_net_records]
                    print(f"{self.grab_window_minutes}分钟内net呼叫数量: {len(call_signs_with_time)}")
                
                # 时间窗口内不足5条（含为空）时：用最近的net记录补足到5条，完全去重呼号（不限时间）
                if len(call_signs_with_time) < 5:
                    print(f"{self.grab_window_minutes}分钟内net呼叫不足5条，用最近记录补足")
                    seen_call_signs = {cs for _, cs in call_signs_with_time}
                    for record in status_info['call_records']:
                        cs = record.get('call_sign', '').strip()
                        if record.get('source', '').lower() == 'net' and cs and cs not in seen_call_signs:
                            seen_call_signs.add(cs)
                            rt = parse_time_str(record.get('time', '')) or current_time
                            call_signs_with_time.append((str(rt), cs))
                        if len(call_signs_with_time) >= 5:
                            break
                    print(f"补足后呼号数量: {len(call_signs_with_time)}")
                    
                print(f"匹配的呼号数量: {len(call_signs_with_time)}")
                
                # 按时间倒序排序（最新的在前）
                call_signs_with_time.sort(reverse=True, key=lambda x: x[0])
                
                # 完全去重呼号（保持最新优先顺序，同一呼号只保留最新一条）
                seen_cs = set()
                unique_call_signs = []
                for _, call_sign in call_signs_with_time:
                    if call_sign and call_sign not in seen_cs:
                        seen_cs.add(call_sign)
                        unique_call_signs.append(call_sign)
                
                print(f"去重后的呼号数量: {len(unique_call_signs)}")
                print(f"唯一呼号列表: {unique_call_signs}")
                print(f"备选框数量: {len(self.alternative_entries)}")
                
                # 清空所有备选框
                for entry in self.alternative_entries:
                    entry.delete(0, tk.END)
                
                # 将去重后的呼号按'用进到远'（最新的在前）的顺序填入备选框
                for i, call_sign in enumerate(unique_call_signs[:len(self.alternative_entries)]):
                    if call_sign and i < len(self.alternative_entries):
                        print(f"填充备选框 {i}: {call_sign}")
                        self.alternative_entries[i].delete(0, tk.END)
                        self.alternative_entries[i].insert(0, call_sign)
        except Exception as e:
            print(f"更新通话记录和备选框时发生错误: {str(e)}")
            # 确保在主线程中显示错误消息
            self.root.after(0, lambda e=e: messagebox.showerror("更新错误", f"填充呼号到备选框时出错: {str(e)}"))
    
    def select_walkie_talkie(self):
        """打开对讲机选择对话框，包含品牌型号和最近使用两个标签页"""
        # 确保可以访问tk模块
        import tkinter as tk
        from tkinter import ttk, messagebox
        import sqlite3
        
        # 创建对讲机选择窗口
        walkie_window = tk.Toplevel(self.root)
        walkie_window.title("选择对讲机型号")
        walkie_window.geometry("600x400")
        walkie_window.transient(self.root)
        walkie_window.grab_set()
        
        # 创建标签页控件
        notebook = ttk.Notebook(walkie_window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 创建'品牌型号'标签页
        brand_model_tab = ttk.Frame(notebook)
        notebook.add(brand_model_tab, text="品牌型号")
        
        # 创建'最近使用'标签页
        recent_tab = ttk.Frame(notebook)
        notebook.add(recent_tab, text="最近使用")
        
        # 品牌型号标签页内容
        # 分割窗口为左右两部分：品牌列表和型号列表
        paned = ttk.PanedWindow(brand_model_tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 品牌列表框架
        brand_frame = ttk.Frame(paned, width=150)
        paned.add(brand_frame, weight=1)
        
        # 型号列表框架
        model_frame = ttk.Frame(paned)
        paned.add(model_frame, weight=3)
        
        # 品牌列表
        ttk.Label(brand_frame, text="品牌").pack(pady=5)
        brand_scroll = ttk.Scrollbar(brand_frame)
        brand_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        brand_listbox = tk.Listbox(brand_frame, yscrollcommand=brand_scroll.set,
                              font=("SimHei", 10), height=15)
        brand_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        brand_scroll.config(command=brand_listbox.yview)
        
        # 型号列表
        ttk.Label(model_frame, text="型号").pack(pady=5)
        model_scroll = ttk.Scrollbar(model_frame)
        model_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        model_listbox = tk.Listbox(model_frame, yscrollcommand=model_scroll.set,
                               font=("SimHei", 10), height=15)
        model_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        model_scroll.config(command=model_listbox.yview)
        
        # 型号搜索框
        search_frame = ttk.Frame(model_frame)
        search_frame.pack(fill=tk.X, pady=5)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT, padx=5)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var)
        search_entry = ttk.Entry(search_frame, textvariable=search_var, width=25)
        search_entry.pack(side=tk.LEFT, padx=5)
        
        # 确保walkie_talkies表存在
        try:
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS walkie_talkies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                UNIQUE(brand, model)
            )
            ''')
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"创建walkie_talkies表失败: {str(e)}")
        
        # 获取所有品牌
        brands = []
        try:
            self.cursor.execute('SELECT DISTINCT brand FROM walkie_talkies ORDER BY brand')
            brands = [row[0] for row in self.cursor.fetchall()]
            print(f"获取到 {len(brands)} 个品牌")
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"获取品牌列表失败: {str(e)}")
        
        # 填充品牌列表
        def fill_brand_list():
            brand_listbox.delete(0, tk.END)
            try:
                self.cursor.execute('SELECT DISTINCT brand FROM walkie_talkies ORDER BY brand')
                brands = [row[0] for row in self.cursor.fetchall()]
                for brand in brands:
                    brand_listbox.insert(tk.END, brand)
            except sqlite3.Error as e:
                print(f"重新加载品牌列表失败: {str(e)}")
        
        fill_brand_list()
        
        # 显示选中品牌的型号
        def show_models(event):
            selected = brand_listbox.curselection()
            if not selected:
                return
            brand = brand_listbox.get(selected[0])
            model_listbox.delete(0, tk.END)
            
            try:
                self.cursor.execute('SELECT model FROM walkie_talkies WHERE brand = ? ORDER BY model', (brand,))
                models = self.cursor.fetchall()
                for row in models:
                    model_listbox.insert(tk.END, row[0])
                print(f"品牌 {brand} 获取到 {len(models)} 个型号")
            except sqlite3.Error as e:
                messagebox.showerror("数据库错误", f"获取型号列表失败: {str(e)}")
        
        # 型号搜索功能
        def search_models(*args):
            search_text = search_var.get().lower()
            selected = brand_listbox.curselection()
            if not selected:
                return
            brand = brand_listbox.get(selected[0])
            
            model_listbox.delete(0, tk.END)
            try:
                self.cursor.execute('SELECT model FROM walkie_talkies WHERE brand = ? AND model LIKE ? ORDER BY model', 
                                  (brand, f'%{search_text}%'))
                models = self.cursor.fetchall()
                for row in models:
                    model_listbox.insert(tk.END, row[0])
                print(f"品牌 {brand} 搜索 '{search_text}' 获取到 {len(models)} 个型号")
            except sqlite3.Error as e:
                messagebox.showerror("数据库错误", f"搜索型号失败: {str(e)}")
        
        # 添加新品牌型号的函数
        def add_new_walkie_talkie():
            # 创建添加对话框
            add_window = tk.Toplevel(walkie_window)
            add_window.title("添加对讲机型号")
            add_window.geometry("300x200")
            add_window.transient(walkie_window)
            add_window.grab_set()
            
            # 添加品牌输入框
            ttk.Label(add_window, text="品牌:").grid(row=0, column=0, padx=10, pady=15, sticky=tk.E)
            new_brand_var = tk.StringVar()
            new_brand_entry = ttk.Entry(add_window, textvariable=new_brand_var, width=20)
            new_brand_entry.grid(row=0, column=1, padx=10, pady=15, sticky=tk.W)
            
            # 添加型号输入框
            ttk.Label(add_window, text="型号:").grid(row=1, column=0, padx=10, pady=15, sticky=tk.E)
            new_model_var = tk.StringVar()
            new_model_entry = ttk.Entry(add_window, textvariable=new_model_var, width=20)
            new_model_entry.grid(row=1, column=1, padx=10, pady=15, sticky=tk.W)
            
            # 保存按钮回调
            def save_new_walkie_talkie():
                new_brand = new_brand_var.get().strip()
                new_model = new_model_var.get().strip()
                
                if not new_brand or not new_model:
                    messagebox.showwarning("警告", "品牌和型号都不能为空")
                    return
                
                try:
                    # 插入新的品牌型号
                    self.cursor.execute('INSERT INTO walkie_talkies (brand, model) VALUES (?, ?)', 
                                      (new_brand, new_model))
                    self.conn.commit()
                    
                    # 刷新品牌列表
                    fill_brand_list()
                    
                    # 如果当前选中的品牌就是新添加的品牌，刷新型号列表
                    selected = brand_listbox.curselection()
                    if selected and brand_listbox.get(selected[0]) == new_brand:
                        show_models(None)
                    
                    messagebox.showinfo("成功", f"已成功添加{new_brand}-{new_model}")
                    add_window.destroy()
                except sqlite3.IntegrityError:
                    messagebox.showerror("错误", f"{new_brand}-{new_model} 已存在")
                except sqlite3.Error as e:
                    messagebox.showerror("数据库错误", f"保存失败: {str(e)}")
            
            # 按钮框架
            btn_frame = ttk.Frame(add_window)
            btn_frame.grid(row=2, column=0, columnspan=2, pady=15)
            
            # 保存和取消按钮
            ttk.Button(btn_frame, text="保存", command=save_new_walkie_talkie).pack(side=tk.LEFT, padx=20)
            ttk.Button(btn_frame, text="取消", command=add_window.destroy).pack(side=tk.LEFT, padx=20)
        
        # 选择型号并返回
        def select_and_close():
            print("[调试] select_and_close函数被调用")
            selected = model_listbox.curselection()
            print(f"[调试] 型号列表选中项: {selected}")
            if selected:
                try:
                    print("[调试] 进入try块")
                    # 获取当前选中的型号
                    model = model_listbox.get(selected[0])
                    print(f"[调试] 选中的型号: {model}")
                    
                    # 获取当前选中的品牌 - 强制获取当前显示型号列表对应的品牌
                    brand_selected = brand_listbox.curselection()
                    print(f"[调试] 品牌列表选中项: {brand_selected}")
                    if brand_selected:
                        brand = brand_listbox.get(brand_selected[0])
                    else:
                        # 强制获取当前显示型号列表对应的品牌（类似于QTH选择中的实现）
                        print("[调试] 品牌列表未选中，强制获取品牌")
                        if brand_listbox.size() > 0:
                            # 尝试找到包含当前型号的品牌
                            brand = ""
                            # 遍历所有品牌
                            for i in range(brand_listbox.size()):
                                brand_candidate = brand_listbox.get(i)
                                try:
                                    # 查询该品牌下的所有型号
                                    self.cursor.execute('SELECT model FROM walkie_talkies WHERE brand = ?', (brand_candidate,))
                                    models = [row[0] for row in self.cursor.fetchall()]
                                    # 检查当前选中的型号是否在该品牌下
                                    if model in models:
                                        brand = brand_candidate
                                        break
                                except sqlite3.Error:
                                    continue
                            
                            # 如果没有找到，使用第一个品牌（应该是当前正在显示型号的品牌）
                            if not brand and brand_listbox.size() > 0:
                                brand = brand_listbox.get(0)
                                print(f"[调试] 使用第一个品牌: {brand}")
                        else:
                            brand = "未知品牌"
                            print("[调试] 没有可用的品牌")
                    
                    print(f"[调试] 最终确定的品牌: {brand}")
                    # 设置设备为"品牌-型号"格式
                    device_text = f"{brand}-{model}"
                    
                    # 简化实现，直接设置StringVar，与QTH选择保持一致
                    print(f"[调试] 准备设置设备值: {device_text}")
                    self.equipment_var.set(device_text)
                    print(f"已选择对讲机: {device_text}")
                    print(f"StringVar内容: {self.equipment_var.get()}")
                    # 添加额外的检查，确认self.equipment_entry和self.equipment_var正确关联
                    print(f"[调试] 设备输入框内容: {self.equipment_entry.get()}")
                    print(f"[调试] 设备输入框textvariable: {self.equipment_entry['textvariable']}")
                except Exception as e:
                    print(f"[错误] 选择对讲机时发生错误: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    messagebox.showerror("错误", f"设置设备输入时发生错误: {str(e)}")
                finally:
                    print("[调试] 准备关闭窗口")
                    walkie_window.destroy()
        
        # 绑定事件
        brand_listbox.bind('<<ListboxSelect>>', show_models)
        search_var.trace_add("write", search_models)
        model_listbox.bind('<Double-1>', lambda e: select_and_close())
        
        # 按钮框架
        btn_frame = ttk.Frame(walkie_window)
        btn_frame.pack(fill=tk.X, padx=5, pady=10)
        
        # 添加按钮
        ttk.Button(btn_frame, text="添加型号", command=add_new_walkie_talkie).pack(side=tk.LEFT, padx=5)
        
        # 确认和取消按钮
        ttk.Button(btn_frame, text="确认", command=select_and_close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=walkie_window.destroy).pack(side=tk.RIGHT, padx=5)
        
        # 默认选中第一个品牌
        if brands:
            brand_listbox.selection_set(0)
            brand_listbox.event_generate('<<ListboxSelect>>')
        
        # 最近使用标签页实现
        def setup_recent_tab():
            """设置最近使用标签页内容"""
            ttk.Label(recent_tab, text="最近使用的设备", font=("SimHei", 10, "bold")).pack(pady=10)
            
            # 创建最近使用设备列表
            recent_scroll = ttk.Scrollbar(recent_tab)
            recent_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            
            recent_listbox = tk.Listbox(recent_tab, yscrollcommand=recent_scroll.set,
                                    font=("SimHei", 10), height=15)
            recent_listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
            recent_scroll.config(command=recent_listbox.yview)
            
            # 获取最近使用的设备（按使用频率排序，限制10个）
            try:
                self.cursor.execute("""
                    SELECT equipment, COUNT(*) as frequency 
                    FROM roll_call_records 
                    WHERE equipment IS NOT NULL AND equipment != '' 
                    GROUP BY equipment 
                    ORDER BY frequency DESC, MAX(record_time) DESC 
                    LIMIT 10
                """)
                recent_equipments = self.cursor.fetchall()
                
                # 填充最近使用列表
                for equipment, freq in recent_equipments:
                    recent_listbox.insert(tk.END, f"{equipment} ({freq}次)")
                
                # 如果没有最近使用记录
                if not recent_equipments:
                    recent_listbox.insert(tk.END, "暂无最近使用记录")
                    recent_listbox.config(state=tk.DISABLED)
            except sqlite3.Error as e:
                print(f"获取最近使用设备错误: {str(e)}")
                recent_listbox.insert(tk.END, "获取最近使用记录失败")
                recent_listbox.config(state=tk.DISABLED)
            
            # 从最近使用列表选择设备
            def select_from_recent(event=None):
                selected = recent_listbox.curselection()
                if selected and recent_listbox.size() > 0 and recent_listbox.get(0) != "暂无最近使用记录":
                    try:
                        # 提取设备名称（去掉使用次数信息）
                        selected_text = recent_listbox.get(selected[0])
                        # 提取括号前的内容作为设备名称
                        if " (" in selected_text:
                            equipment_name = selected_text.split(" (")[0]
                        else:
                            equipment_name = selected_text
                        
                        # 设置设备值
                        self.equipment_var.set(equipment_name)
                        walkie_window.destroy()
                    except Exception as e:
                        print(f"选择最近使用设备错误: {str(e)}")
            
            # 绑定双击事件
            recent_listbox.bind('<Double-1>', select_from_recent)
            
            # 添加确认按钮
            ttk.Button(recent_tab, text="确认选择", command=select_from_recent).pack(pady=10)
        
        # 初始化最近使用标签页
        setup_recent_tab()
            
    def select_city(self):
        """打开城市选择对话框"""
        # 创建城市选择窗口
        city_window = tk.Toplevel(self.root)
        city_window.title("选择城市")
        # 设置窗口大小和位置（相对于父窗口居中）
        city_window.geometry("500x500+%d+%d" % (
            self.root.winfo_x() + (self.root.winfo_width() - 500) // 2,
            self.root.winfo_y() + (self.root.winfo_height() - 500) // 2
        ))
        city_window.transient(self.root)
        city_window.grab_set()
        
        # 分割窗口为左右两部分：省份列表和城市列表
        paned = ttk.PanedWindow(city_window, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 省份列表框架
        province_frame = ttk.Frame(paned, width=150)
        paned.add(province_frame, weight=1)
        
        # 城市列表框架
        city_frame = ttk.Frame(paned)
        paned.add(city_frame, weight=3)
        
        # 省份列表
        ttk.Label(province_frame, text="省份/地区").pack(pady=5)
        province_scroll = ttk.Scrollbar(province_frame)
        province_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        province_listbox = tk.Listbox(province_frame, yscrollcommand=province_scroll.set, 
                                      font=("SimHei", 10), height=15)
        province_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        province_scroll.config(command=province_listbox.yview)
        
        # 填充省份列表
        for province in self.provinces:
            province_listbox.insert(tk.END, province)
        
        # 城市列表
        ttk.Label(city_frame, text="城市").pack(pady=5)
        city_scroll = ttk.Scrollbar(city_frame)
        city_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        city_listbox = tk.Listbox(city_frame, yscrollcommand=city_scroll.set,
                                 font=("SimHei", 10), height=15)
        city_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        city_scroll.config(command=city_listbox.yview)
        
        # 城市搜索框
        search_frame = ttk.Frame(city_frame)
        search_frame.pack(fill=tk.X, pady=5)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT, padx=5)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var, width=25)
        search_entry.pack(side=tk.LEFT, padx=5)

        # 创建区县列表框
        district_label = ttk.Label(city_window, text="区县：")
        district_label.pack(anchor=tk.W, padx=5, pady=5)
        
        district_frame = ttk.Frame(city_window)
        district_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        district_listbox = tk.Listbox(district_frame, width=40, height=8)
        district_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar_district = ttk.Scrollbar(district_frame, orient=tk.VERTICAL, command=district_listbox.yview)
        scrollbar_district.pack(side=tk.RIGHT, fill=tk.Y)
        district_listbox.configure(yscroll=scrollbar_district.set)

        # 显示选中省份的城市
        def show_cities(event):
            selected = province_listbox.curselection()
            if not selected:
                return
            province = province_listbox.get(selected[0])
            city_listbox.delete(0, tk.END)
            for city in self.city_list.get(province, []):
                city_listbox.insert(tk.END, city)
            
            # 清空区县列表
            district_listbox.delete(0, tk.END)
        
        # 城市搜索功能
        def search_cities(*args):
            search_text = search_var.get().lower()
            selected = province_listbox.curselection()
            if not selected:
                return
            province = province_listbox.get(selected[0])
            
            city_listbox.delete(0, tk.END)
            for city in self.city_list.get(province, []):
                if search_text in city.lower():
                    city_listbox.insert(tk.END, city)
        
        # 显示选中城市的区县
        def show_districts(event):
            print("Debug: show_districts函数被调用")
            
            # 清空区县列表
            district_listbox.delete(0, tk.END)
            
            # 获取选中的省份和城市
            province_selected = province_listbox.curselection()
            city_selected = city_listbox.curselection()
            
            print(f"Debug: province_listbox选中状态={province_selected}")
            print(f"Debug: city_listbox选中状态={city_selected}")
            
            province = ""
            city = ""
            
            # 首先尝试获取选中的城市
            if city_selected:
                city = city_listbox.get(city_selected[0])
                print(f"Debug: 成功获取选中的城市={city}")
            elif hasattr(show_districts, 'last_selected_city'):
                # 优先使用最后选中的城市
                last_city = getattr(show_districts, 'last_selected_city', '')
                # 检查最后选中的城市是否在当前城市列表中
                cities = []
                for i in range(city_listbox.size()):
                    cities.append(city_listbox.get(i))
                if last_city in cities:
                    city = last_city
                    print(f"Debug: 未选中城市，使用最后选中的城市={city}")
                    # 自动选中最后选中的城市
                    for i, c in enumerate(cities):
                        if c == city:
                            city_listbox.selection_set(i)
                            city_listbox.see(i)
                            break
                else:
                    # 如果最后选中的城市不在当前列表中，使用第一个城市
                    city = city_listbox.get(0)
                    print(f"Debug: 未选中城市，最后选中的城市不在当前列表中，使用第一个城市={city}")
            elif city_listbox.size() > 0:
                # 优先使用第一个城市，避免在初始化时依赖可能不存在的属性
                city = city_listbox.get(0)
                print(f"Debug: 未选中城市，使用第一个城市={city}")
            else:
                print("Debug: 没有可用的城市数据")
                return
            
            # 获取选中的省份
            if province_selected:
                province = province_listbox.get(province_selected[0])
            else:
                # 强制获取省份（如果有选中的城市）
                if city and province_listbox.size() > 0:
                    # 查找哪个省份包含当前选择的城市
                    for i in range(province_listbox.size()):
                        prov = province_listbox.get(i)
                        if prov in self.city_list and city in self.city_list[prov]:
                            province = prov
                            break
                    else:
                        # 如果找不到，使用第一个省份
                        province = province_listbox.get(0)
                    print(f"Debug: 强制获取省份={province}")
                else:
                    province = ""
            
            # 初始化或更新last_selected_city属性
            if not hasattr(show_districts, 'last_selected_city'):
                setattr(show_districts, 'last_selected_city', city)
                print(f"Debug: 初始化last_selected_city={city}")
            else:
                setattr(show_districts, 'last_selected_city', city)
                print(f"Debug: 更新last_selected_city={city}")
            
            # 调试输出检查district_map是否存在
            print(f"Debug: district_map是否存在: {hasattr(self, 'district_map')}")
            if hasattr(self, 'district_map'):
                print(f"Debug: district_map的类型: {type(self.district_map)}")
                print(f"Debug: district_map的键数量: {len(self.district_map) if isinstance(self.district_map, dict) else '不是字典'}")
            
            # 尝试获取区县数据并填充列表
            if hasattr(self, 'district_map') and isinstance(self.district_map, dict):
                # 检查当前省份和城市的键是否在district_map中
                key = (province, city)
                print(f"Debug: 检查键: {key} 是否在district_map中")
                
                if key in self.district_map:
                    districts = self.district_map[key]
                    print(f"Debug: 找到区县数据，数量: {len(districts)}")
                    for district in districts:
                        district_listbox.insert(tk.END, district)
                    print(f"Debug: 成功加载{len(districts)}个区县数据")
                else:
                    # 如果没有找到，尝试使用备用键格式（去除市后缀）
                    city_without_suffix = city.replace("市", "").replace("自治州", "").replace("地区", "")
                    alternative_key = (province, city_without_suffix)
                    print(f"Debug: 尝试备用键格式: {alternative_key}")
                    
                    if alternative_key in self.district_map:
                        districts = self.district_map[alternative_key]
                        print(f"Debug: 使用备用键找到区县数据，数量: {len(districts)}")
                        for district in districts:
                            district_listbox.insert(tk.END, district)
                        print(f"Debug: 成功加载{len(districts)}个区县数据")
                    else:
                        # 如果没有区县数据，添加提示信息
                        print("Debug: 未找到区县数据，显示'暂无区县数据'")
                        district_listbox.insert(tk.END, "暂无区县数据")
            else:
                # 如果district_map不存在或不是字典，创建它
                print("Debug: district_map不存在或不是字典，创建新的district_map")
                self.district_map = {}
                district_listbox.insert(tk.END, "暂无区县数据")

        # 区县选择事件处理
        def on_district_select(event):
            selected = district_listbox.curselection()
            if selected:
                selected_district = district_listbox.get(selected[0])
                print(f"Debug: 用户选中区县={selected_district}")
                # 保存选中的区县
                on_district_select.selected_district = selected_district
            else:
                # 清除保存的区县
                if hasattr(on_district_select, 'selected_district'):
                    delattr(on_district_select, 'selected_district')

        # 选择城市并返回
        def select_and_close():
            selected = city_listbox.curselection()
            if selected:
                # 获取当前选中的城市
                city = city_listbox.get(selected[0])
                
                # 获取当前选中的省份 - 强制获取当前显示城市列表对应的省份
                province_selected = province_listbox.curselection()
                if province_selected:
                    province = province_listbox.get(province_selected[0])
                else:
                    # 强制选择第一个省份（应该是当前正在显示城市的省份）
                    if province_listbox.size() > 0:
                        # 查找哪个省份包含当前选择的城市
                        for i in range(province_listbox.size()):
                            prov = province_listbox.get(i)
                            if prov in self.city_list and city in self.city_list[prov]:
                                province = prov
                                break
                        else:
                            # 如果找不到，使用第一个省份
                            province = province_listbox.get(0)
                        print(f"Debug: 强制获取省份={province}")
                    else:
                        province = ""
                        print("Debug: 没有可用的省份")
                    
                # 添加调试信息，查看实际的值
                print(f"Debug: 省份={province}, 城市={city}")
                
                # 尝试获取选中的区县
                district_selected = district_listbox.curselection()
                district = ""
                # 优先使用on_district_select保存的区县
                if hasattr(on_district_select, 'selected_district'):
                    district = on_district_select.selected_district
                    print(f"Debug: 使用保存的选中区县={district}")
                elif district_selected:
                    district = district_listbox.get(district_selected[0])
                    print(f"Debug: 成功获取选中的区县={district}")
                else:
                    print("Debug: 未选中区县")
                    # 如果没有选中区县，不自动选择，保持为空字符串
                    print("Debug: 不自动选择区县，保持为空")
                
                # 对于直辖市等特殊情况，只显示城市名
                special_cities = ['北京市', '上海市', '天津市', '重庆市']
                if city in special_cities:
                    if district and district != "暂无区县数据":
                        # 特殊城市带区县：城市-区县
                        qth_parts = [city, district]
                        print(f"Debug: 特殊城市带区县，设置QTH={'-'.join(qth_parts)}")
                        self.qth_var.set('-'.join(qth_parts))
                    else:
                        print(f"Debug: 特殊城市，设置QTH={city}")
                        self.qth_var.set(city)
                else:
                    # 非特殊城市：始终包含省份、城市，可选区县
                    qth_parts = [province, city]
                    if district and district != "暂无区县数据":
                        qth_parts.append(district)
                    
                    # 使用连字符连接各部分
                    full_qth = '-'.join(qth_parts)
                    print(f"Debug: 非特殊城市，设置QTH={full_qth}")
                    self.qth_var.set(full_qth)
                city_window.destroy()
        
        # 绑定事件
        province_listbox.bind('<<ListboxSelect>>', show_cities)
        search_var.trace_add("write", search_cities)
        city_listbox.bind('<<ListboxSelect>>', show_districts)
        city_listbox.bind('<Double-1>', lambda e: select_and_close())
        district_listbox.bind('<<ListboxSelect>>', on_district_select)
        district_listbox.bind('<Double-1>', lambda e: select_and_close())
        
        # 确认和取消按钮
        btn_frame = ttk.Frame(city_window)
        btn_frame.pack(fill=tk.X, padx=5, pady=10)
        
        ttk.Button(btn_frame, text="确认", command=select_and_close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=city_window.destroy).pack(side=tk.RIGHT, padx=5)
        
        # 默认选中第一个省份
        if self.provinces:
            province_listbox.selection_set(0)
            province_listbox.event_generate('<<ListboxSelect>>')
    
    def on_operator_call_sign_change(self, *args):
        """当主控呼号发生变化时，将其转换为大写并过滤中文字符"""
        # 如果是程序自动更新，不执行处理逻辑，防止循环
        if self.updating_operator_call_sign:
            return
            
        operator_call_sign = self.operator_call_sign_var.get().strip()
        
        # 过滤中文字符（保留非中文字符）
        # 使用正则表达式匹配中文字符
        import re
        filtered_call_sign = re.sub(r'[\u4e00-\u9fff]+', '', operator_call_sign)
        
        # 如果过滤后与原字符串不同，则更新
        if filtered_call_sign != operator_call_sign:
            operator_call_sign = filtered_call_sign
        
        # 如果主控呼号不为空且包含小写字母，则转换为大写
        if operator_call_sign and operator_call_sign != operator_call_sign.upper():
            self.updating_operator_call_sign = True  # 设置标志防止循环
            self.operator_call_sign_var.set(operator_call_sign.upper())
            self.updating_operator_call_sign = False  # 重置标志
        # 如果只是过滤了中文字符，也需要更新
        elif filtered_call_sign != self.operator_call_sign_var.get().strip():
            self.updating_operator_call_sign = True  # 设置标志防止循环
            self.operator_call_sign_var.set(filtered_call_sign)
            self.updating_operator_call_sign = False  # 重置标志
            
    def on_call_sign_change(self, *args):
        """当呼号发生变化时，从数据库查询并填充历史信息，同时将呼号转换为大写，识别呼号前缀并填充省份信息"""
        # 如果是程序自动更新，不执行处理逻辑，防止循环
        if self.updating_call_sign:
            return
            
        call_sign = self.call_sign_var.get().strip()
        
        # 如果呼号不为空且包含小写字母，则转换为大写
        if call_sign and call_sign != call_sign.upper():
            self.updating_call_sign = True  # 设置标志防止循环
            self.call_sign_var.set(call_sign.upper())
            self.updating_call_sign = False  # 重置标志
            # 更新后重新获取大写的呼号
            call_sign = self.call_sign_var.get().strip()
        
        if not call_sign:
            self.history_tooltip.config(text="")
            # 清空省份信息
            self.province_var.set("")
            return
            
        # 使用呼号前缀识别省份
        try:
            import call_sign_province_mapper
            recognized_province = call_sign_province_mapper.call_sign_province_mapper.recognize_province(call_sign)
            # 设置省份信息
            self.province_var.set(recognized_province if recognized_province else "")
        except Exception as e:
            print(f"识别省份时发生错误: {str(e)}")
            self.province_var.set("")
            
        try:
            # 首先从call_sign_library表中查找精确匹配
            self.cursor.execute('''
            SELECT qth, equipment, power, access_method, antenna 
            FROM call_sign_library 
            WHERE call_sign = ?
            ''', (call_sign,))
            
            result = self.cursor.fetchone()
            
            if result:
                qth, equipment, power, access_method, antenna = result
                # 填充从呼号库获取的信息，确保QTH格式使用连字符分隔
                if qth:
                    parts = qth.replace(' ', '-').split('-')
                    parts = [part.strip() for part in parts if part.strip()]
                    formatted_qth = '-'.join(parts)
                    self.qth_var.set(formatted_qth)
                else:
                    self.qth_var.set("")
                self.equipment_var.set(equipment)
                self.power_var.set(power)  # 填充功率信息
                # 回填接入方式信息
                self.access_method_var.set(access_method if access_method else "")
                # 回填天馈参数信息
                if hasattr(self, 'antenna_var'):
                    self.antenna_var.set(antenna if antenna else "")
                # 显示提示信息
                self.history_tooltip.config(
                    text=f"已加载呼号库信息: {call_sign}"
                )
            else:
                # 如果呼号库中未找到，再从call_signs历史表中查找
                self.cursor.execute('''
                SELECT qth, equipment, power, access_method, last_updated 
                FROM call_signs 
                WHERE call_sign = ?
                ''', (call_sign,))
                
                result = self.cursor.fetchone()
                
                if result:
                    qth, equipment, power, access_method, last_updated = result
                    # 填充历史信息，包括功率，确保QTH格式使用连字符分隔
                    if qth:
                        parts = qth.replace(' ', '-').split('-')
                        parts = [part.strip() for part in parts if part.strip()]
                        formatted_qth = '-'.join(parts)
                        self.qth_var.set(formatted_qth)
                    else:
                        self.qth_var.set("")
                    self.equipment_var.set(equipment)
                    self.power_var.set(power)  # 填充功率信息
                    self.access_method_var.set(access_method)
                    # 显示提示信息
                    self.history_tooltip.config(
                        text=f"已加载历史记录 (最后更新: {last_updated})"
                    )
                else:
                    # 查找相似匹配，提供建议
                    self.cursor.execute('''
                    SELECT call_sign 
                    FROM call_sign_library 
                    WHERE call_sign LIKE ?
                    LIMIT 5
                    ''', (f'%{call_sign}%',))
                    
                    suggestions = self.cursor.fetchall()
                    if suggestions:
                        suggested = ", ".join([s[0] for s in suggestions])
                        self.history_tooltip.config(
                            text=f"未找到精确匹配，相似呼号: {suggested}"
                        )
                    else:
                        self.history_tooltip.config(text="无呼号库和历史记录")
                    
        except sqlite3.Error as e:
            self.history_tooltip.config(text=f"数据库查询错误: {str(e)}")
    
    def on_qth_change(self, *args):
        """处理QTH输入变化，实现拼音自动转换为城市名称，支持声母简写和模糊匹配，按省、市、区层级匹配，并有下拉选择功能"""
        # 防止转换循环
        if hasattr(self, 'qth_updating') and self.qth_updating:
            return
        
        qth_text = self.qth_var.get().strip()
        
        # 如果已经是中文，不进行转换
        if self.is_chinese(qth_text):
            # 检查是否已经包含省份信息（有两个以上的中文字符）
            # 这可以防止通过选择城市设置的完整信息被拼音转换覆盖
            # 但如果是包含空格的输入（如"lys "），需要允许空格识别逻辑执行
            if len(qth_text) > 2 and ' ' not in qth_text:
                return
        
        # 如果已经存在下拉列表，先销毁它
        if hasattr(self, 'qth_dropdown') and self.qth_dropdown is not None and self.qth_dropdown.winfo_exists():
            self.qth_dropdown.destroy()
        
        # 获取当前呼号确定的省份信息
        determined_province = self.province_var.get().strip()
        print(f"呼号确定的省份: {determined_province}")
        
        # 定义常见省份的缩写映射（从数据库中得知的）
        # 包含3字母和2字母缩写，支持2+2、2+3、3+2等不同匹配格式
        province_abbrs = {
            # 3字母缩写
            'lns': '辽宁省', 'bjs': '北京市', 'shs': '上海市',
            'tjs': '天津市', 'cqs': '重庆市',
            'jss': '江苏省', 'zjs': '浙江省', 'gds': '广东省',
            'sds': '山东省', 'hns': '河南省', 'scs': '四川省',
            'hbs': '河北省', 'hljs': '黑龙江省',  # 修正: 根据数据库实际存储，hbs代表河北省
            'fjs': '福建省', 'ahs': '安徽省',
            'sxs': '山西省',
            # 2字母缩写，支持2+2、2+3的匹配格式
            'ln': '辽宁省', 'bj': '北京市', 'sh': '上海市',
            'tj': '天津市', 'cq': '重庆市',
            'js': '江苏省', 'zj': '浙江省', 'gd': '广东省',
            'sd': '山东省', 'hn': '河南省', 'sc': '四川省',
            'hb': '河北省', 'hlj': '黑龙江省', 'fj': '福建省', 'ah': '安徽省',
            'sx': '山西省'
        }
        
        # 定义直辖市列表
        municipalities = ['北京市', '上海市', '天津市', '重庆市']
        
        # 省份拼音映射表
        self.province_pinyin_map = {
            '北京市': 'bjs', '天津市': 'tjs', '河北省': 'hbs', '山西省': 'sxs', '内蒙古自治区': 'nmgzzq',
            '辽宁省': 'lns', '吉林省': 'jls', '黑龙江省': 'hljs', '上海市': 'shs', '江苏省': 'jss',
            '浙江省': 'zjs', '安徽省': 'ahs', '福建省': 'fjs', '江西省': 'jxs', '山东省': 'sds',
            '河南省': 'hns', '湖北省': 'hbs', '湖南省': 'hns', '广东省': 'gds', '广西壮族自治区': 'gxzzzzq',
            '海南省': 'hns', '重庆市': 'cqs', '四川省': 'scs', '贵州省': 'gzs', '云南省': 'yns',
            '西藏自治区': 'xzzzq', '陕西省': 'sxs', '甘肃省': 'gss', '青海省': 'qhs', '宁夏回族自治区': 'nxhzzzq',
            '新疆维吾尔自治区': 'xjwwezzq', '台湾省': 'tws', '香港特别行政区': 'xgtbxzq', '澳门特别行政区': 'amtbxzq'
        }
        
        # 新增：空格键触发城市识别规则（放在is_pinyin检查之前）
        # 如果输入包含空格，将空格前的部分识别为城市，空格后的部分识别为区县
        print(f"DEBUG: 检查输入是否包含空格: '{qth_text}'")
        if ' ' in qth_text:
            print("=== 进入空格识别逻辑 ===")
            print("DEBUG: 输入包含空格，开始处理空格识别")
            print(f"DEBUG: 当前省份: '{self.province_var.get().strip()}'")
            input_str = qth_text.lower()
            parts = input_str.split(' ', 1)
            city_part = parts[0].strip()
            district_part = parts[1].strip() if len(parts) > 1 else ''
            
            print(f"空格分隔规则: 城市部分='{city_part}', 区县部分='{district_part}'")
            
            if city_part:
                # 先匹配城市
                try:
                    conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                    cursor = conn.cursor()
                    
                    # 获取当前省份信息
                    current_province = self.province_var.get().strip()
                    print(f"当前省份: '{current_province}'")
                    
                    # 只有在当前省份有值时才进行查询
                    if not current_province:
                        print("没有省份信息，不进行城市查询")
                        conn.close()
                        return
                    
                    # 在当前省份内查询匹配的城市
                    # 确保只匹配城市级别的数据：区县字段为空字符串
                    cursor.execute("""
                        SELECT DISTINCT 省, 市 FROM qth_data 
                        WHERE 首字母 LIKE ? AND 省 = ? AND 市 IS NOT NULL AND (省/市/区 IS NULL OR 省/市/区 = '')
                        LIMIT 5
                    """, (f'%{city_part}%', current_province))
                    print(f"在当前省份 '{current_province}' 内查询城市: {city_part} (查询条件: %{city_part}%)")
                    
                    city_matches = cursor.fetchall()
                    print(f"数据库查询结果: 查询条件='{city_part}', 匹配数量={len(city_matches)}")
                    for i, match in enumerate(city_matches):
                        print(f"  匹配{i+1}: {match}")
                    
                    if city_matches:
                        # 优先选择城市名称包含拼音的城市（如lys优先选择辽阳市）
                        selected_city = None
                        for province, city in city_matches:
                            # 简单拼音匹配：检查城市名称是否包含拼音
                            city_lower = city.lower()
                            if city_part in city_lower:
                                selected_city = (province, city)
                                break
                        
                        # 如果没有找到包含拼音的城市，取第一个匹配的城市
                        if not selected_city:
                            selected_city = city_matches[0]
                        
                        province_name, city_name = selected_city
                        print(f"空格规则找到匹配城市: {city_part} -> {province_name}-{city_name} (从{len(city_matches)}个匹配中选出)")
                        
                        if district_part:
                            # 如果还有区县部分，匹配区县
                            district_matches = self.match_districts_in_city(district_part, province_name, city_name)
                            
                            if district_matches:
                                print(f"空格规则找到匹配区县: {district_part} -> {len(district_matches)}个结果")
                                # 如果只有一个匹配结果，直接更新QTH输入框
                                if len(district_matches) == 1:
                                    self.qth_updating = True
                                    matched_district = district_matches[0][3]  # 获取完整的省-市-区县格式
                                    # 确保使用连字符分隔
                                    parts = matched_district.replace(' ', '-').split('-')
                                    parts = [part.strip() for part in parts if part.strip()]
                                    final_qth = '-'.join(parts)
                                    self.qth_var.set(final_qth)
                                    self.qth_updating = False
                                    self.history_tooltip.config(text=f"已自动填充: {input_str} → {matched_district}")
                                    conn.close()
                                    return
                                else:
                                    # 多个匹配结果，显示下拉选择
                                    self.handle_qth_matches(district_matches, input_str)
                                    conn.close()
                                    return
                            else:
                                # 只有城市匹配，没有区县匹配，设置省-市格式
                                self.qth_updating = True
                                self.qth_var.set(f"{province_name}-{city_name}")
                                self.qth_updating = False
                                self.history_tooltip.config(text=f"已识别城市: {city_part} → {province_name}-{city_name}")
                                conn.close()
                                return
                        else:
                            # 只有城市部分，没有区县部分，设置省-市格式
                            self.qth_updating = True
                            self.qth_var.set(f"{province_name}-{city_name}")
                            self.qth_updating = False
                            self.history_tooltip.config(text=f"已识别城市: {city_part} → {province_name}-{city_name}")
                            conn.close()
                            return
                    
                    conn.close()
                    return  # 空格规则处理完成，返回
                except Exception as e:
                    print(f"空格规则匹配时发生错误: {e}")
                    # 即使发生错误也要返回，避免继续执行后续逻辑
                    return
        
        # 检查是否为拼音或声母简写，尝试转换
        if self.is_pinyin(qth_text):
            # 转换为小写
            input_str = qth_text.lower()
            
            print(f"开始处理拼音输入: {input_str}")
            
            # 新增：如果呼号已确定省份，尝试直接匹配区县拼音
            if determined_province and len(input_str) >= 2:
                print(f"DEBUG: 尝试直接匹配区县拼音，省份: {determined_province}, 输入: {input_str}")
                
                # 在当前省份的所有城市中匹配区县
                try:
                    conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                    cursor = conn.cursor()
                    
                    # 查询当前省份下所有包含该区县拼音的城市
                    cursor.execute("""
                        SELECT DISTINCT 省, 市, "省/市/区", 首字母 FROM qth_data 
                        WHERE 省 = ? AND "省/市/区" IS NOT NULL AND "省/市/区" != ''
                        AND (首字母 LIKE ? OR 首字母 LIKE ?)
                        ORDER BY "省/市/区"
                    """, (determined_province, f'%{input_str}%', f'{input_str}%'))
                    
                    district_matches = cursor.fetchall()
                    print(f"DEBUG: 直接区县匹配结果: {len(district_matches)} 个匹配")
                    
                    if district_matches:
                        # 处理匹配结果
                        matches = []
                        for province_name, city_name, district, initials in district_matches:
                            if district and district.strip():
                                # 计算匹配评分
                                if initials.startswith(input_str):
                                    score = 0  # 前缀匹配得分最高
                                elif input_str in initials:
                                    score = 1  # 包含匹配次之
                                else:
                                    score = 2  # 其他匹配
                                
                                # 直接使用"省/市/区"字段的完整内容，避免重复拼接
                                matches.append((score, len(initials), initials, district))
                        
                        # 按评分排序
                        matches.sort(key=lambda x: (x[0], x[1], len(x[2])))
                        
                        if matches:
                            # 如果只有一个匹配结果，直接更新QTH输入框
                            if len(matches) == 1:
                                self.qth_updating = True
                                matched_district = matches[0][3]  # 获取完整的省-市-区县格式
                                # 确保使用连字符分隔
                                parts = matched_district.replace(' ', '-').split('-')
                                parts = [part.strip() for part in parts if part.strip()]
                                final_qth = '-'.join(parts)
                                self.qth_var.set(final_qth)
                                self.qth_updating = False
                                self.history_tooltip.config(text=f"已自动填充区县: {input_str} → {matched_district}")
                                conn.close()
                                return
                            else:
                                # 多个匹配结果，显示下拉选择
                                self.handle_qth_matches(matches, input_str)
                                conn.close()
                                return
                    
                    conn.close()
                except Exception as e:
                    print(f"直接区县匹配时发生错误: {e}")
            
            # 检查当前QTH是否已经包含城市信息（优先处理区县匹配）
            current_qth = self.qth_var.get().strip()
            if '-' in current_qth and len(current_qth.split('-')) >= 2:
                # 已经有省-市格式，尝试匹配区县
                parts = current_qth.split('-')
                if len(parts) >= 2:
                    province_part = parts[0]
                    city_part = parts[1]
                    # 检查是否还有区县部分需要匹配
                    if len(parts) == 2:  # 只有省-市，没有区县
                        print(f"DEBUG: 进入区县匹配逻辑，当前QTH: {current_qth}, 输入拼音: {input_str}")
                        print(f"在城市 {city_part} 内匹配区县")
                        district_matches = self.match_districts_in_city(input_str, province_part, city_part)
                        print(f"DEBUG: 区县匹配结果: {len(district_matches)} 个匹配")
                        
                        if district_matches:
                            # 如果只有一个匹配结果，直接更新QTH输入框
                            if len(district_matches) == 1:
                                self.qth_updating = True
                                matched_district = district_matches[0][3]  # 获取完整的省-市-区县格式
                                self.qth_var.set(matched_district)
                                self.qth_updating = False
                                self.history_tooltip.config(text=f"已自动填充区县: {input_str} → {matched_district}")
                                return
                            else:
                                # 多个匹配结果，显示下拉选择
                                self.handle_qth_matches(district_matches, input_str)
                                return
                        else:
                            print(f"在城市 {city_part} 内未找到匹配的区县")
            
            # 新增：支持一次性输入城市声母简写+区县声母简写的组合（如lybtq）
            # 尝试将输入拆分为城市部分和区县部分进行匹配
            if len(input_str) >= 3:  # 至少需要城市简写(2-3字符)+区县简写(1-3字符)
                print(f"尝试拆分组合简写: {input_str}")
                # 城市简写长度：2-3字符，区县简写长度：1-3字符
                for city_len in range(2, 4):  # 城市简写长度2-3
                    for district_len in range(1, 4):  # 区县简写长度1-3
                        if city_len + district_len <= len(input_str):
                            city_part_abbr = input_str[:city_len]
                            district_part_abbr = input_str[city_len:city_len+district_len]
                            print(f"尝试拆分: 城市简写={city_part_abbr}, 区县简写={district_part_abbr}")
                            
                            # 使用数据库查询来匹配城市简写
                            try:
                                conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                                cursor = conn.cursor()
                                
                                # 查询匹配城市简写的记录（只在当前省份内查询）
                                current_province = self.province_var.get().strip()
                                if current_province:
                                    cursor.execute("""
                                        SELECT DISTINCT 省, 市 FROM qth_data 
                                        WHERE 首字母 LIKE ? AND 省 = ? AND 市 IS NOT NULL
                                        LIMIT 5
                                    """, (f'%{city_part_abbr}%', current_province))
                                else:
                                    # 如果没有省份信息，不进行匹配
                                    conn.close()
                                    continue
                                
                                city_matches = cursor.fetchall()
                                
                                if city_matches:
                                    # 取第一个匹配的城市
                                    province_name, city_name = city_matches[0]
                                    print(f"找到匹配城市: {city_part_abbr} -> {province_name}-{city_name}")
                                    
                                    # 在匹配到的城市内匹配区县简写
                                    district_matches = self.match_districts_in_city(district_part_abbr, province_name, city_name)
                                    
                                    if district_matches:
                                        print(f"找到匹配区县: {district_part_abbr} -> {len(district_matches)}个结果")
                                        # 如果只有一个匹配结果，直接更新QTH输入框
                                        if len(district_matches) == 1:
                                            self.qth_updating = True
                                            matched_district = district_matches[0][3]  # 获取完整的省-市-区县格式
                                            self.qth_var.set(matched_district)
                                            self.qth_updating = False
                                            self.history_tooltip.config(text=f"已自动填充: {input_str} → {matched_district}")
                                            conn.close()
                                            return
                                        else:
                                            # 多个匹配结果，显示下拉选择
                                            self.handle_qth_matches(district_matches, input_str)
                                            conn.close()
                                            return
                                
                                conn.close()
                            except Exception as e:
                                print(f"组合简写匹配时发生错误: {e}")
            
            # 如果呼号已确定省份，优先在该省份内匹配城市
            if determined_province:
                print(f"在省份 {determined_province} 内匹配城市")
                
                # 在省份内匹配城市
                matches = self.match_cities_in_province(input_str, determined_province)
                
                # 在省份内匹配城市
                matches = self.match_cities_in_province(input_str, determined_province)
                
                # 如果在确定的省份内找到匹配，直接使用
                if matches:
                    self.handle_qth_matches(matches, input_str)
                    return
                else:
                    print(f"在省份 {determined_province} 内未找到匹配，继续全局匹配")
            
            # 如果没有确定省份或在确定省份内未找到匹配，继续原有逻辑
            # 对于单个字母的输入，不直接进行精确匹配，而是显示所有以该字母开头的城市选项
            # 这样用户可以从下拉列表中选择，而不是直接定位到单个城市
            if len(input_str) == 1:
                pass  # 不执行精确匹配，继续下面的模糊匹配逻辑
            else:
                # 精确匹配
                if input_str in self.city_pinyin_map:
                    self.qth_updating = True
                    city_name = self.city_pinyin_map[input_str]
                    # 确保城市名包含省份前缀和分隔符
                    if '-' not in city_name:
                        # 尝试从数据库中获取省份信息
                        try:
                            conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                            cursor = conn.cursor()
                            cursor.execute('SELECT DISTINCT 省 FROM qth_data WHERE 市 = ? LIMIT 1', (city_name,))
                            result = cursor.fetchone()
                            if result:
                                province_name = result[0]
                                # 确保使用'-'作为分隔符
                                qth_parts = [province_name, city_name]
                                city_name = '-'.join(qth_parts)
                            conn.close()
                        except Exception as e:
                            print(f"获取省份信息失败: {e}")
                    
                    # 清理任何可能存在的空格，确保使用连字符
                    city_name = city_name.replace(' ', '-')
                    self.qth_var.set(city_name)
                    self.qth_updating = False
                    self.history_tooltip.config(text=f"已自动转换: {input_str} → {city_name}")
                    return
            
            # 模糊匹配
            matches = []
            
            # 获取当前已输入的省市信息，用于层级匹配
            current_qth = self.qth_var.get().strip()
            selected_province = None
            selected_city = None
            
            # 检查当前QTH中是否已经包含省份或城市信息
            for province in self.provinces:
                if province in current_qth:
                    selected_province = province
                    # 查找该省份下的城市
                    for city in self.city_list.get(province, []):
                        if city in current_qth:
                            selected_city = city
                            break
                    break
            
            # 优化: 组合拼音匹配 (例如'lnslys' 对应 '辽宁省辽阳市')
            # 直接利用数据库中的首字母信息进行匹配
            if not matches and len(input_str) >= 4:
                
                try:
                    # 连接数据库
                    conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                    cursor = conn.cursor()
                    
                    # 处理输入字符串，转换为适合SQL LIKE查询的模式
                    # 特别是对于'lnslys'这样的输入，我们需要查找包含'lns'和'lys'的记录
                    input_str_lower = input_str.lower()
                    
                    found_province = None
                    city_part = None
                    abbr_found = None
                    
                    # 先处理特殊格式（如'lnly'、'lnsy'等）
                    # 这些特殊格式不需要严格遵循省+城市的结构
                    found_province = None
                    city_part = None
                    abbr_found = None
                    
                    # 1. 检查是否有特殊城市缩写匹配（如'ly'、'lys'、'sy'、'sys'）
                    special_city_match = None
                    if 'ly' in input_str_lower or 'lys' in input_str_lower:
                        special_city_match = '辽阳市'
                        # 优先查找3字母缩写
                        for abbr in ['lns']:
                            if abbr in input_str_lower:
                                found_province = '辽宁省'
                                abbr_found = abbr
                                city_part = input_str_lower.replace(abbr, '', 1).strip()
                                break
                        # 如果没找到3字母缩写，查找2字母缩写
                        if not found_province:
                            for abbr in ['ln']:
                                if abbr in input_str_lower:
                                    found_province = '辽宁省'
                                    abbr_found = abbr
                                    city_part = input_str_lower.replace(abbr, '', 1).strip()
                                    break
                    elif 'sy' in input_str_lower or 'sys' in input_str_lower or 'lnsy' in input_str_lower:
                        special_city_match = '沈阳市'
                        # 优先查找3字母缩写
                        for abbr in ['lns']:
                            if abbr in input_str_lower:
                                found_province = '辽宁省'
                                abbr_found = abbr
                                city_part = input_str_lower.replace(abbr, '', 1).strip()
                                break
                        # 如果没找到3字母缩写，查找2字母缩写
                        if not found_province:
                            for abbr in ['ln']:
                                if abbr in input_str_lower:
                                    found_province = '辽宁省'
                                    abbr_found = abbr
                                    city_part = input_str_lower.replace(abbr, '', 1).strip()
                                    break
                    else:
                        # 2. 检查输入是否包含已知的省份缩写，并确保有额外的城市部分
                        # 优先检查3字母缩写
                        for abbr in sorted(province_abbrs.keys(), key=len, reverse=True):
                            if abbr in input_str_lower:
                                # 提取缩写后的部分作为城市部分
                                city_part = input_str_lower.replace(abbr, '', 1).strip()
                                # 确保有城市部分且长度至少为1
                                if len(city_part) >= 1:
                                    found_province = province_abbrs[abbr]
                                    abbr_found = abbr
                                    break
                    
                    # 3. 对于直辖市，允许直接匹配城市部分
                    if not found_province:
                        for abbr, province in province_abbrs.items():
                            if province in municipalities and abbr in input_str_lower:
                                # 直辖市可以直接匹配，不需要严格的省+城市结构
                                found_province = province
                                abbr_found = abbr
                                city_part = input_str_lower
                                break
                    
                    # 如果找到了省份和城市部分，查询该省份下匹配城市部分的记录
                    if found_province and city_part:
                        print(f"  找到省份: {found_province} (缩写: {abbr_found})")
                        print(f"  提取的城市部分: '{city_part}' (长度: {len(city_part)})")
                        
                        # 对于辽宁省的特殊处理，支持2+2、2+3、3+2、3+3等不同格式
                        if found_province == '辽宁省':
                            # 构建SQL查询，支持精确匹配不同城市的拼音组合
                            # 改进点：通过更精确的匹配条件区分'lnly'和'lncy'，避免互相干扰
                            sql_query = """
                                SELECT DISTINCT 省, 市 FROM qth_data 
                                WHERE 省 = ? 
                                AND 市 IS NOT NULL 
                                AND (
                                    -- 精确匹配组合格式：省份+城市
                                    (首字母 LIKE ? AND 首字母 NOT LIKE ?)  -- 完整匹配但排除特定组合
                                    OR 
                                    -- 对于2+2格式的优化匹配（如ln+ly, ln+cy等）
                                    (首字母 LIKE ? AND 市 LIKE ?)
                                )
                                LIMIT 20
                            """
                            
                            # 提取城市部分
                            city_part_lower = city_part.lower()
                            
                            # 构建更精确的匹配模式，避免不同城市间的拼音冲突
                            pattern_complete = f'%{abbr_found}%{city_part}%'
                            
                            # 针对'ly'和'cy'的特殊排除逻辑，确保它们不会互相干扰
                            if city_part_lower == 'ly':
                                pattern_exclude = '%cys lys%'  # 排除朝阳市包含'lys'的情况
                                specific_city = '辽阳市'
                            elif city_part_lower == 'cy':
                                pattern_exclude = '%lys%'  # 排除辽阳市包含'lys'的情况
                                specific_city = '朝阳市'
                            else:
                                pattern_exclude = '%'
                                specific_city = '%'
                            
                            # 构建特定的匹配模式
                            pattern_specific = f'%{abbr_found[:2]}%{city_part[:2]}%'
                            
                            # 执行查询
                            cursor.execute(sql_query, (found_province, pattern_complete, pattern_exclude, pattern_specific, specific_city))
                            print(f"  应用辽宁优化匹配模式: {pattern_complete}, 排除: {pattern_exclude}, 特定城市: {specific_city}")
                        else:
                            # 构建通用的SQL查询，支持2+2、2+3、3+2、3+3等不同格式
                            sql_query = """
                                SELECT DISTINCT 省, 市 FROM qth_data 
                                WHERE 省 = ? 
                                AND 市 IS NOT NULL 
                                AND (
                                    首字母 LIKE ? 
                                    OR 首字母 LIKE ?
                                    OR 首字母 LIKE ?
                                    OR 首字母 LIKE ?
                                    OR 首字母 LIKE ?
                                )
                                LIMIT 20
                            """
                            
                            # 构建多种匹配模式
                            pattern1 = f'%{abbr_found}%{city_part}%'  # 完整匹配
                            pattern2 = f'%{abbr_found[:2]}%{city_part}%' if len(abbr_found) == 3 else f'%{abbr_found}%{city_part[:2]}%'  # 2+2或2+3格式
                            pattern3 = f'%{city_part}%'  # 只匹配城市部分
                            pattern4 = f'%{abbr_found}%{city_part[:2]}%'  # 对于3+2格式
                            pattern5 = f'%{abbr_found[:2]}%{city_part[:2]}%'  # 对于2+2格式
                            
                            # 执行查询
                            cursor.execute(sql_query, (found_province, pattern1, pattern2, pattern3, pattern4, pattern5))
                            print(f"  应用多模式匹配: {pattern1}, {pattern2}, {pattern3}, {pattern4}, {pattern5}")
                        
                        results = cursor.fetchall()
                        for province, city in results:
                            if city and city.strip():
                                matches.append((-1, len(input_str), input_str, f"{province}-{city}"))
                    
                    # 如果没有找到特定省份的匹配，尝试在当前省份内进行更通用的查询
                    if not matches:
                        # 获取当前省份信息
                        current_province = self.province_var.get().strip()
                        if current_province:
                            # 构建通用的SQL查询，查找首字母中包含输入字符串的记录
                            patterns = [
                                f'%{input_str_lower}%',  # 原始模式
                                f'%{input_str_lower[:3]}%{input_str_lower[3:]}%' if len(input_str_lower) > 3 else None
                            ]
                            
                            # 执行所有有效的查询模式（只在当前省份内查询）
                            for pattern in patterns:
                                if pattern:
                                    cursor.execute("""
                                        SELECT DISTINCT 省, 市 FROM qth_data 
                                        WHERE 省 = ? AND 市 IS NOT NULL 
                                        AND 首字母 LIKE ? 
                                        LIMIT 20
                                    """, (current_province, pattern))
                                    
                                    results = cursor.fetchall()
                                    for province, city in results:
                                        if city and city.strip():
                                            matches.append((-1, len(input_str), input_str, f"{province}-{city}"))
                        
                        # 如果还没有匹配，尝试针对特定组合的特殊处理
                        if 'lnslys' in input_str_lower or 'lnly' in input_str_lower:
                            cursor.execute("""
                                SELECT DISTINCT 省, 市 FROM qth_data 
                                WHERE 省 = '辽宁省' AND 市 = '辽阳市'
                                LIMIT 1
                            """)
                            result = cursor.fetchone()
                            if result:
                                province, city = result
                                matches.append((-1, len(input_str), input_str, f"{province}-{city}"))
                        elif 'lnsy' in input_str_lower or 'lnsys' in input_str_lower:
                            cursor.execute("""
                                SELECT DISTINCT 省, 市 FROM qth_data 
                                WHERE 省 = '辽宁省' AND 市 = '沈阳市'
                                LIMIT 1
                            """)
                            result = cursor.fetchone()
                            if result:
                                province, city = result
                                matches.append((-1, len(input_str), input_str, f"{province}-{city}"))
                    
                    # 去重匹配结果
                    seen = set()
                    unique_matches = []
                    for match in matches:
                        if match[3] not in seen:
                            seen.add(match[3])
                            unique_matches.append(match)
                    matches = unique_matches
                    
                except Exception as e:
                    # 防止数据库操作错误影响整体功能
                    print(f"组合拼音匹配时数据库操作出错: {e}")
                finally:
                    if 'conn' in locals():
                        conn.close()
            
            # 新增: 改进的声母处理和模糊匹配逻辑
            # 1. 增强的声母简写匹配（支持3个以上字母的特殊情况）
            # 层级匹配逻辑
            if selected_province and selected_city:
                # 如果已经选择了省和市，只匹配区县
                for district in self.district_map.get((selected_province, selected_city), []):
                    district_pinyin = district.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                    syllables = re.findall(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+', district_pinyin)
                    initials = ''.join([s[0] for s in syllables])
                    # 同时检查startswith和包含关系
                    if initials.startswith(input_str) or input_str in initials:
                        # 计算匹配评分：完全匹配得分最高，前缀匹配次之，包含匹配最后
                        score = 0 if initials == input_str else (1 if initials.startswith(input_str) else 2)
                        matches.append((score, len(initials), district_pinyin, f"{selected_province}-{selected_city}{district}"))
            elif selected_province:
                # 如果已经选择了省，只匹配该省下的市
                for city in self.city_list.get(selected_province, []):
                    city_pinyin = city.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                    syllables = re.findall(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+', city_pinyin)
                    initials = ''.join([s[0] for s in syllables])
                    # 同时检查startswith和包含关系
                    if initials.startswith(input_str) or input_str in initials:
                        # 计算匹配评分
                        score = 0 if initials == input_str else (1 if initials.startswith(input_str) else 2)
                        matches.append((score, len(initials), city_pinyin, f"{selected_province}-{city}"))
            else:
                # 如果没有选择省，只匹配直辖市
                for abbr, province in province_abbrs.items():
                    if province in municipalities:
                        # 直接匹配直辖市
                        if input_str in abbr or abbr in input_str:
                            # 获取直辖市下的城市列表（通常只有一个）
                            for city in self.city_list.get(province, []):
                                matches.append((2, 0, input_str, f"{province}-{city}"))
                                break
                        else:
                            # 尝试匹配直辖市内的区
                            for city in self.city_list.get(province, []):
                                for district in self.district_map.get((province, city), []):
                                    district_pinyin = district.replace("市", "").replace("区", "").lower()
                                    syllables = re.findall(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+', district_pinyin)
                                    initials = ''.join([s[0] for s in syllables])
                                    if initials.startswith(input_str) or input_str in initials:
                                        matches.append((2, len(initials), district_pinyin, f"{province}-{city}{district}"))
                                        break
                            if matches:
                                break
                        
            # 2. 增强的拼音前缀匹配
            if not matches:
                if selected_province and selected_city:
                    # 如果已经选择了省和市，只匹配区县
                    for district in self.district_map.get((selected_province, selected_city), []):
                        district_pinyin = district.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                        if district_pinyin.startswith(input_str):
                            matches.append((3, len(district_pinyin), district_pinyin, f"{selected_province}-{selected_city}{district}"))
                elif selected_province:
                    # 如果已经选择了省，只匹配该省下的市
                    for city in self.city_list.get(selected_province, []):
                        city_pinyin = city.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                        if city_pinyin.startswith(input_str):
                            matches.append((3, len(city_pinyin), city_pinyin, f"{selected_province}-{city}"))
                else:
                    # 如果没有选择省，只匹配直辖市
                    for abbr, province in province_abbrs.items():
                        if province in municipalities and input_str in abbr:
                            # 获取直辖市下的城市列表（通常只有一个）
                            for city in self.city_list.get(province, []):
                                matches.append((3, len(abbr), input_str, f"{province}-{city}"))
                                break
            
            # 3. 增强的拼音包含匹配
            if not matches:
                if selected_province and selected_city:
                    # 如果已经选择了省和市，只匹配区县
                    for district in self.district_map.get((selected_province, selected_city), []):
                        district_pinyin = district.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                        if input_str in district_pinyin:
                            matches.append((4, district_pinyin.index(input_str), district_pinyin, f"{selected_province}-{selected_city}{district}"))
                elif selected_province:
                    # 如果已经选择了省，只匹配该省下的市
                    for city in self.city_list.get(selected_province, []):
                        city_pinyin = city.replace("市", "").replace("自治州", "").replace("地区", "").lower()
                        if input_str in city_pinyin:
                            matches.append((4, city_pinyin.index(input_str), city_pinyin, f"{selected_province}-{city}"))
                else:
                    # 如果没有选择省，只匹配直辖市
                    for abbr, province in province_abbrs.items():
                        if province in municipalities and input_str in abbr:
                            # 获取直辖市下的城市列表（通常只有一个）
                            for city in self.city_list.get(province, []):
                                matches.append((4, 0, input_str, f"{province}-{city}"))
                                break
            
            # 对匹配结果排序（使用多维度评分系统）
            matches.sort(key=lambda x: (x[0], x[1], len(x[2])))  # 按评分、匹配位置/长度、拼音长度排序
            
            # 处理匹配结果
            if matches:
                # 提取唯一的城市名称
                unique_matches = {}  
                for score, _, _, city_name in matches:
                    if city_name not in unique_matches or score < unique_matches[city_name][0]:
                        unique_matches[city_name] = (score, city_name)
                
                # 按评分排序唯一匹配结果
                sorted_unique = sorted(unique_matches.values(), key=lambda x: x[0])
                
                # 准备显示的匹配结果列表
                display_matches = [city_name for _, city_name in sorted_unique]
                
                # 如果只有一个匹配结果，自动填充到输入框
                if len(sorted_unique) == 1:
                    self.qth_updating = True
                    matched_city = sorted_unique[0][1]
                    self.qth_var.set(matched_city)
                    self.qth_updating = False
                    self.history_tooltip.config(text=f"已自动填充: {input_str} → {matched_city}")
                else:
                    # 显示下拉列表供选择
                    try:
                        # 销毁已存在的下拉列表
                        if hasattr(self, 'qth_dropdown') and self.qth_dropdown.winfo_exists():
                            self.qth_dropdown.destroy()
                        
                        # 创建下拉列表窗口
                        self.qth_dropdown = tk.Toplevel(self.root)
                        self.qth_dropdown.overrideredirect(True)  # 无边框窗口
                        
                        # 获取QTH输入框的位置，用于定位下拉列表
                        x = self.qth_entry.winfo_rootx()
                        y = self.qth_entry.winfo_rooty() + self.qth_entry.winfo_height()
                        self.qth_dropdown.geometry(f"{self.qth_entry.winfo_width()}x{min(200, len(display_matches) * 25)}+{x}+{y}")
                        
                        # 创建列表框
                        listbox = tk.Listbox(self.qth_dropdown, font=('SimHei', 10))
                        listbox.pack(fill=tk.BOTH, expand=True)
                        
                        # 填充列表框
                        for item in display_matches:
                            listbox.insert(tk.END, item)
                        
                        # 绑定选择事件
                        def on_select(event=None):
                            if listbox.curselection():
                                selected_item = listbox.get(listbox.curselection()[0])
                                self.qth_updating = True
                                self.qth_var.set(selected_item)
                                self.qth_updating = False
                                if hasattr(self, 'qth_dropdown') and self.qth_dropdown.winfo_exists():
                                    self.qth_dropdown.destroy()
                        
                        listbox.bind('<Double-1>', on_select)
                        listbox.bind('<Return>', on_select)
                        
                        # 点击其他地方关闭下拉列表
                        def on_click_outside(event):
                            if not hasattr(self, 'qth_dropdown') or not self.qth_dropdown.winfo_exists():
                                return
                            x_win, y_win = self.qth_dropdown.winfo_rootx(), self.qth_dropdown.winfo_rooty()
                            width, height = self.qth_dropdown.winfo_width(), self.qth_dropdown.winfo_height()
                            if not (x_win <= event.x_root <= x_win + width and y_win <= event.y_root <= y_win + height):
                                self.qth_dropdown.destroy()
                        
                        self.root.bind('<Button-1>', on_click_outside)
                        
                    except Exception as e:
                        # 出错时静默处理，避免影响用户体验
                        print(f"QTH下拉列表创建错误: {str(e)}")
            else:
                # 没有匹配时显示更友好的提示
                if not self.history_tooltip.cget("text").startswith("已加载历史记录"):
                    self.history_tooltip.config(text="未找到匹配的城市，请尝试其他拼音或首字母")
    
    def match_cities_in_province(self, input_str, province):
        """在指定省份内匹配城市"""
        matches = []
        
        try:
            # 连接数据库
            conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
            cursor = conn.cursor()
            
            # 查询指定省份下的城市，按拼音首字母匹配
            cursor.execute("""
                SELECT DISTINCT 省, 市, 首字母 FROM qth_data 
                WHERE 省 = ? AND 市 IS NOT NULL 
                AND (首字母 LIKE ? OR 首字母 LIKE ?)
                ORDER BY 市
            """, (province, f'%{input_str}%', f'{input_str}%'))
            
            results = cursor.fetchall()
            for province_name, city, initials in results:
                if city and city.strip():
                    # 计算匹配评分
                    if initials.startswith(input_str):
                        score = 0  # 前缀匹配得分最高
                    elif input_str in initials:
                        score = 1  # 包含匹配次之
                    else:
                        score = 2  # 其他匹配
                    
                    matches.append((score, len(initials), initials, f"{province_name}-{city}"))
            
            # 按评分排序
            matches.sort(key=lambda x: (x[0], x[1], len(x[2])))
            
        except Exception as e:
            print(f"在省份内匹配城市时发生错误: {e}")
        finally:
            if 'conn' in locals():
                conn.close()
        
        return matches
    
    def handle_qth_matches(self, matches, input_str):
        """处理QTH匹配结果"""
        if not matches:
            return
        
        # 提取唯一的城市名称
        unique_matches = {}  
        for score, _, _, city_name in matches:
            if city_name not in unique_matches or score < unique_matches[city_name][0]:
                unique_matches[city_name] = (score, city_name)
        
        # 按评分排序唯一匹配结果
        sorted_unique = sorted(unique_matches.values(), key=lambda x: x[0])
        
        # 准备显示的匹配结果列表
        display_matches = [city_name for _, city_name in sorted_unique]
        
        # 如果只有一个匹配结果，自动填充到输入框
        if len(sorted_unique) == 1:
            self.qth_updating = True
            matched_city = sorted_unique[0][1]
            # 确保城市名包含省份前缀和分隔符
            if '-' not in matched_city:
                # 尝试从数据库中获取省份信息
                try:
                    conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
                    cursor = conn.cursor()
                    cursor.execute('SELECT DISTINCT 省 FROM qth_data WHERE 市 = ? LIMIT 1', (matched_city,))
                    result = cursor.fetchone()
                    if result:
                        province_name = result[0]
                        matched_city = f"{province_name}-{matched_city}"
                    conn.close()
                except Exception as e:
                    print(f"获取省份信息失败: {e}")
            
            self.qth_var.set(matched_city)
            self.qth_updating = False
            self.history_tooltip.config(text=f"已自动填充: {input_str} → {matched_city}")
        else:
            # 显示下拉列表供选择
            self.show_qth_dropdown(display_matches)
    
    def show_qth_dropdown(self, display_matches):
        """显示QTH下拉选择列表"""
        try:
            # 销毁已存在的下拉列表
            if hasattr(self, 'qth_dropdown') and self.qth_dropdown is not None and self.qth_dropdown.winfo_exists():
                self.qth_dropdown.destroy()
            
            # 创建下拉列表窗口
            self.qth_dropdown = tk.Toplevel(self.root)
            self.qth_dropdown.overrideredirect(True)  # 无边框窗口
            
            # 获取QTH输入框的位置，用于定位下拉列表
            x = self.qth_entry.winfo_rootx()
            y = self.qth_entry.winfo_rooty() + self.qth_entry.winfo_height()
            self.qth_dropdown.geometry(f"{self.qth_entry.winfo_width()}x{min(200, len(display_matches) * 25)}+{x}+{y}")
            
            # 创建列表框
            listbox = tk.Listbox(self.qth_dropdown, font=('SimHei', 10))
            listbox.pack(fill=tk.BOTH, expand=True)
            
            # 填充列表框
            for item in display_matches:
                listbox.insert(tk.END, item)
            
            # 绑定选择事件
            def on_select(event=None):
                if listbox.curselection():
                    selected_item = listbox.get(listbox.curselection()[0])
                    self.qth_updating = True
                    self.qth_var.set(selected_item)
                    self.qth_updating = False
                    if hasattr(self, 'qth_dropdown') and self.qth_dropdown.winfo_exists():
                        self.qth_dropdown.destroy()
            
            listbox.bind('<Double-1>', on_select)
            listbox.bind('<Return>', on_select)
            
            # 点击其他地方关闭下拉列表
            def on_click_outside(event):
                if not hasattr(self, 'qth_dropdown') or not self.qth_dropdown.winfo_exists():
                    return
                x_win, y_win = self.qth_dropdown.winfo_rootx(), self.qth_dropdown.winfo_rooty()
                width, height = self.qth_dropdown.winfo_width(), self.qth_dropdown.winfo_height()
                if not (x_win <= event.x_root <= x_win + width and y_win <= event.y_root <= y_win + height):
                    self.qth_dropdown.destroy()
            
            self.root.bind('<Button-1>', on_click_outside)
            
        except Exception as e:
            # 出错时静默处理，避免影响用户体验
            print(f"QTH下拉列表创建错误: {str(e)}")
    
    def match_districts_in_city(self, input_str, province, city):
        """在指定城市内匹配区县"""
        matches = []
        
        try:
            # 连接数据库
            conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
            cursor = conn.cursor()
            
            # 查询指定省份和城市下的区县，按拼音首字母匹配
            cursor.execute("""
                SELECT DISTINCT 省, 市, "省/市/区", 首字母 FROM qth_data 
                WHERE 省 = ? AND 市 = ? AND "省/市/区" IS NOT NULL AND "省/市/区" != ''
                AND (首字母 LIKE ? OR 首字母 LIKE ?)
                ORDER BY "省/市/区"
            """, (province, city, f'%{input_str}%', f'{input_str}%'))
            
            results = cursor.fetchall()
            for province_name, city_name, district, initials in results:
                if district and district.strip():
                    # 计算匹配评分
                    if initials.startswith(input_str):
                        score = 0  # 前缀匹配得分最高
                    elif input_str in initials:
                        score = 1  # 包含匹配次之
                    else:
                        score = 2  # 其他匹配
                    
                    matches.append((score, len(initials), initials, f"{province_name}-{city_name}-{district}"))
            
            # 按评分排序
            matches.sort(key=lambda x: (x[0], x[1], len(x[2])))
            
        except Exception as e:
            print(f"在城市内匹配区县时发生错误: {e}")
        finally:
            if 'conn' in locals():
                conn.close()
        
        return matches
    
    def on_access_method_change(self, *args):
        """处理接入方式输入变化，实现数字自动转换为对应文本，不显示额外提示"""
        # 如果是程序自动更新，不执行处理逻辑，防止循环
        if self.updating_access_method:
            return
            
        access_text = self.access_method_var.get().strip()
        
        # 获取当前台网类型
        network_type = self.network_type_var.get()
        
        # 根据台网类型限制可输入的数字范围
        valid_numbers = []
        if network_type == "数字台网":
            valid_numbers = ["1", "2", "3", "4"]
        else:  # 模拟台网
            valid_numbers = ["4", "5", "6", "7", "8", "9"]
        
        # 检查是否为数字且在有效范围内
        if access_text in valid_numbers:
            self.updating_access_method = True  # 设置标志防止循环
            # 转换为对应的文本
            self.access_method_var.set(self.access_method_map[access_text])
            self.updating_access_method = False  # 重置标志
        elif access_text and access_text.isdigit():
            # 输入了数字但不在有效范围内，清空输入
            self.updating_access_method = True
            self.access_method_var.set("")
            self.updating_access_method = False
    
    def on_equipment_change(self, *args):
        """处理设备输入变化，实现数字输入2位后显示下拉列表供选择"""
        # 防止循环调用
        if hasattr(self, 'equipment_updating') and self.equipment_updating:
            return
        
        # 如果已经存在下拉列表，先销毁它
        if hasattr(self, 'equipment_dropdown') and self.equipment_dropdown.winfo_exists():
            self.equipment_dropdown.destroy()
        
        equipment_text = self.equipment_var.get().strip()
        
        # 如果输入为空，不进行处理
        if not equipment_text:
            return
        
        # 检查是否至少有2位字符（包括字母和数字）
        if len(equipment_text) >= 2:
            try:
                # 收集所有匹配的设备
                all_matches = []
                
                # 1. 从call_sign_library表中查找匹配
                self.cursor.execute("""
                    SELECT DISTINCT equipment FROM call_sign_library 
                    WHERE equipment LIKE ? OR equipment LIKE ? OR equipment LIKE ?
                    ORDER BY equipment
                """, (f"%{equipment_text}%", f"%{equipment_text}-%", f"%-{equipment_text}%"))
                exact_matches = self.cursor.fetchall()
                all_matches.extend([row[0] for row in exact_matches])
                
                # 2. 从walkie_talkies表中查找匹配
                self.cursor.execute("""
                    SELECT DISTINCT brand || '-' || model FROM walkie_talkies 
                    WHERE model LIKE ?
                    ORDER BY brand || '-' || model
                """, (f"%{equipment_text}%",))
                walkie_matches = self.cursor.fetchall()
                all_matches.extend([row[0] for row in walkie_matches])
                
                # 去重
                all_matches = list(set(all_matches))
                all_matches.sort()
                
                # 如果有匹配结果，显示下拉列表
                if all_matches:
                    # 创建下拉列表窗口
                    self.equipment_dropdown = tk.Toplevel(self.root)
                    self.equipment_dropdown.overrideredirect(True)  # 无边框窗口
                    
                    # 获取设备输入框的位置，用于定位下拉列表
                    x = self.equipment_entry.winfo_rootx()
                    y = self.equipment_entry.winfo_rooty() + self.equipment_entry.winfo_height()
                    self.equipment_dropdown.geometry(f"{self.equipment_entry.winfo_width()}x{min(200, len(all_matches) * 25)}+{x}+{y}")
                    
                    # 创建列表框
                    listbox = tk.Listbox(self.equipment_dropdown, font=("SimHei", 10))
                    listbox.pack(fill=tk.BOTH, expand=True)
                    
                    # 填充列表框
                    for item in all_matches:
                        listbox.insert(tk.END, item)
                    
                    # 绑定选择事件
                    def on_select(event=None):
                        if listbox.curselection():
                            selected_item = listbox.get(listbox.curselection()[0])
                            self.equipment_updating = True
                            self.equipment_var.set(selected_item)
                            self.equipment_updating = False
                            self.equipment_dropdown.destroy()
                    
                    listbox.bind('<Double-1>', on_select)
                    listbox.bind('<Return>', on_select)
                    
                    # 点击其他地方关闭下拉列表
                    def on_click_outside(event):
                        if not self.equipment_dropdown.winfo_exists():
                            return
                        x, y = self.equipment_dropdown.winfo_rootx(), self.equipment_dropdown.winfo_rooty()
                        width, height = self.equipment_dropdown.winfo_width(), self.equipment_dropdown.winfo_height()
                        if not (x <= event.x_root <= x + width and y <= event.y_root <= y + height):
                            self.equipment_dropdown.destroy()
                    
                    self.root.bind('<Button-1>', on_click_outside)
                    
            except sqlite3.Error as e:
                # 出错时静默处理，避免影响用户体验
                print(f"设备匹配查询错误: {str(e)}")
            finally:
                # 确保标志被重置
                if hasattr(self, 'equipment_updating'):
                    self.equipment_updating = False
        
        # 非数字输入或未找到匹配时，不做处理
        
    def is_chinese(self, text):
        """判断文本是否包含中文字符"""
        return bool(re.search('[\u4e00-\u9fff]', text))
    
    def is_pinyin(self, text):
        """判断文本是否为拼音或声母简写（仅包含字母和声调符号）"""
        # 匹配完整拼音或声母简写（允许单个或多个字母）
        return bool(re.fullmatch(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+', text))
    
    def on_tree_select(self, event):
        """当选择表格中的记录时，回填信息到输入框，包括功率和天馈参数信息"""
        selected_item = self.tree.selection()
        if selected_item:
            item = self.tree.item(selected_item[0])
            values = item['values']
            if values:
                # 回填呼号时确保是大写
                self.call_sign_var.set(values[1].upper())
                self.qth_var.set(values[2])        # QTH
                self.equipment_var.set(values[3])  # 设备
                self.power_var.set(values[4])      # 功率
                self.antenna_var.set(values[5])  # 天馈参数
                self.access_method_var.set(values[6])  # 接入方式
    
    def add_record(self):
        # 获取输入值，确保呼号是大写
        call_sign = self.call_sign_var.get().strip().upper()
        qth = self.qth_var.get().strip()
        equipment = self.equipment_var.get().strip()
        power = self.power_var.get().strip()  # 获取功率值
        
        # 根据网络类型处理天馈参数
        network_type = self.network_type_var.get()
        if network_type == "模拟台网":
            antenna = self.antenna_var.get().strip()  # 模拟台网：获取天馈参数
        else:
            antenna = ""  # 数字台网：天馈参数为空
        
        access_method = self.access_method_var.get().strip()
        main_control = self.operator_call_sign_var.get().strip()  # 获取主控呼号
        station_network = self.station_name_var.get().strip()  # 获取台网点名
        
        # 验证输入
        if not call_sign:
            messagebox.showerror("错误", "呼号不能为空!")
            self.call_sign_entry.focus()
            return
        
        # 检查呼号是否已存在（排除主控行）
        for child in self.tree.get_children():
            item = self.tree.item(child)
            # 跳过主控行
            if item['values'][0] == "主控":
                continue
            # 检查呼号是否已存在
            if item['values'][1] == call_sign:
                messagebox.showerror("错误", f"呼号 {call_sign} 已经存在，不能重复记录!")
                self.call_sign_entry.focus()
                return
        
        # 验证接入方式
        if not access_method:
            messagebox.showerror("错误", "接入方式不能为空!")
            self.access_method_entry.focus()
            return
            
        # 检查接入方式是否有效（是映射表中的值或数字）
        valid_access = access_method in self.access_method_map.values() or access_method in self.access_method_map.keys()
        if not valid_access:
            messagebox.showerror("错误", "接入方式无效，请输入1、2或3")
            self.access_method_entry.focus()
            return
        
        # 移除功率验证，允许任意字符输入
        
        try:
            # 确保存储的是转换后的接入方式文本
            if access_method in self.access_method_map:
                access_method_text = self.access_method_map[access_method]
            else:
                access_method_text = access_method
            
            # 先检查呼号是否已存在，存在则更新，不存在则插入，包含功率字段
            self.cursor.execute('SELECT id FROM call_signs WHERE call_sign = ?', (call_sign,))
            if self.cursor.fetchone():
                # 更新现有记录，包含功率信息、主控、台网和天馈参数字段
                self.cursor.execute('''
                UPDATE call_signs 
                SET qth = ?, equipment = ?, power = ?, antenna = ?, access_method = ?, main_control = ?, station_network = ?, last_updated = ?
                WHERE call_sign = ?
                ''', (qth, equipment, power, antenna, access_method_text, main_control, station_network, datetime.now(), call_sign))
            else:
                # 插入新记录，包含功率信息、主控、台网和天馈参数字段
                self.cursor.execute('''
                INSERT INTO call_signs 
                (call_sign, qth, equipment, power, antenna, access_method, main_control, station_network, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (call_sign, qth, equipment, power, antenna, access_method_text, main_control, station_network, datetime.now()))
            
            # 检查主控行是否为空
            master_is_empty = False
            master_item = None
            for child in self.tree.get_children():
                item = self.tree.item(child)
                if item['values'][0] == "主控":
                    master_item = child
                    # 检查主控行的呼号（索引1）是否为空
                    if not item['values'][1] or item['values'][1].strip() == "":
                        master_is_empty = True
                    break
            
            # 如果主控行为空，将当前输入填入主控行，不占用名次
            if master_is_empty and master_item:
                # 检查主控行呼号是否与台网信息主控呼号不同
                operator_call_sign = self.operator_call_sign_var.get().strip()
                if operator_call_sign and call_sign != operator_call_sign:
                    messagebox.showwarning(
                        "主控呼号不正确", 
                        f"主控行呼号 ({call_sign}) 与台网信息主控呼号 ({operator_call_sign}) 不同。\n请重新填写正确的呼号。"
                    )
                    # 不填入主控行，让用户重新填写
                    return
                
                # 更新主控行的值
                self.tree.item(master_item, values=("主控", call_sign, qth, equipment, power, antenna, access_method_text))
                
                # 不添加到records列表，不占用名次
                # 不保存到roll_call_records数据库
                
            else:
                # 主控行不为空或不存在，正常添加记录
                # 获取当前时间，只显示时分秒（不显示日期）
                confirm_time_full = datetime.now()  # 完整时间，用于数据库存储
                confirm_time_display = confirm_time_full.strftime("%H:%M")  # 仅时间，用于显示（到分钟）
                
                # 添加到记录列表，包含功率、天馈参数、主控呼号和台网
                record = (
                    self.current_rank,
                    call_sign,
                    qth,
                    equipment,
                    power,  # 功率信息
                    access_method_text,  # 存储转换后的文本
                    antenna,  # 天馈参数
                    confirm_time_display,
                    confirm_time_full.strftime("%Y-%m-%d %H:%M:%S"),  # 完整时间，用于内部处理
                    main_control,  # 主控呼号
                    station_network  # 台网点名
                )
                self.records.append(record)
                
                # 添加到表格，插入到主控行之后（索引1），包含确认时间
                self.tree.insert("", 1, values=(record[0], record[1], record[2], record[3], record[4], record[6], record[5], record[7]), tags=('time_tag',))
                
                # 保存到数据库，包含功率和天馈参数
                # 点名记录存在则更新，不存在则插入
                # 以呼号+确认时间为唯一键
                self.cursor.execute('SELECT id FROM roll_call_records WHERE call_sign = ? AND confirm_time = ?', 
                                   (call_sign, confirm_time_full.strftime("%Y-%m-%d %H:%M:%S")))
                existing = self.cursor.fetchone()
                if existing:
                    self.cursor.execute('''
                        UPDATE roll_call_records
                        SET rank = ?, qth = ?, equipment = ?, power = ?, antenna = ?, access_method = ?
                        WHERE id = ?
                    ''', (self.current_rank, qth, equipment, power, antenna, access_method_text, existing[0]))
                else:
                    self.cursor.execute('''
                        INSERT INTO roll_call_records 
                        (rank, call_sign, qth, equipment, power, antenna, access_method, confirm_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (self.current_rank, call_sign, qth, equipment, power, antenna, access_method_text, 
                          confirm_time_full.strftime("%Y-%m-%d %H:%M:%S")))
                
                # 更新当前名次
                self.current_rank += 1
            
            # 同步到call_sign_library表：如果呼号已存在则更新，不存在则插入
            self.cursor.execute('''
            INSERT OR REPLACE INTO call_sign_library 
            (call_sign, qth, equipment, power, antenna, access_method)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', (call_sign, qth, equipment, power, antenna, access_method_text))
            
            # 同时写入到主控呼号的数据库中
            operator_call_sign = self.operator_call_sign_var.get().strip()
            if operator_call_sign:
                try:
                    db_filename = os.path.join(APP_DIR, f"{operator_call_sign}.db")
                    # 检查数据库文件是否存在
                    if os.path.exists(db_filename):
                        # 连接到主控呼号的数据库
                        operator_conn = sqlite3.connect(db_filename)
                        operator_cursor = operator_conn.cursor()
                        
                        # 写入或更新数据到主控呼号数据库的call_sign_library表
                        operator_cursor.execute('''
                        INSERT OR REPLACE INTO call_sign_library 
                        (call_sign, qth, equipment, power, antenna, access_method)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ''', (call_sign, qth, equipment, power, antenna, access_method_text))
                        
                        # 提交并关闭连接
                        operator_conn.commit()
                        operator_conn.close()
                except sqlite3.Error as e:
                    # 主控数据库操作失败不应影响主功能，仅记录错误
                    print(f"[警告] 写入上传数据库失败: {str(e)}")
            
            self.conn.commit()
            
            # 滚动到主控行附近，确保总是显示主控行和最新记录
            # 先更新界面，然后滚动到主控行
            self.root.update_idletasks()
            # 查找主控行并滚动到主控行位置
            for child in self.tree.get_children():
                item = self.tree.item(child)
                if item['values'][0] == "主控":
                    self.tree.see(child)
                    break
            
            # 清空输入框，准备下一次输入
            self.call_sign_var.set("")
            self.qth_var.set("")
            self.equipment_var.set("")
            self.power_var.set("")  # 清空功率输入框
            self.antenna_var.set("")  # 清空天馈参数输入框
            self.access_method_var.set("")  # 清空接入方式输入框
            self.call_sign_entry.focus()
            
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("数据库错误", f"添加记录失败: {str(e)}")
    
    def show_history(self):
        """显示历史记录管理窗口，包含功率列"""
        history_window = tk.Toplevel(self.root)
        history_window.title("历史记录管理")
        history_window.geometry("900x500")
        history_window.transient(self.root)
        history_window.grab_set()
        
        # 创建表格，包含功率列
        columns = ("呼号", "QTH", "设备", "功率(W)", "接入方式", "最后更新")
        tree = ttk.Treeview(history_window, columns=columns, show="headings")
        
        # 设置列宽和标题
        column_widths = [120, 150, 120, 80, 120, 180]
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=column_widths[i], anchor=tk.CENTER)
        
        # 为历史记录中的时间列应用放大字体
        tree.tag_configure('time_tag', font=("SimHei", 12, "bold"))
        
        # 添加滚动条
        scrollbar_y = ttk.Scrollbar(history_window, orient=tk.VERTICAL, command=tree.yview)
        scrollbar_x = ttk.Scrollbar(history_window, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscroll=scrollbar_y.set, xscroll=scrollbar_x.set)
        
        # 加载历史数据，包含功率信息
        try:
            self.cursor.execute('''
            SELECT call_sign, qth, equipment, power, access_method, last_updated 
            FROM call_signs 
            ORDER BY last_updated DESC
            ''')
            
            for row in self.cursor.fetchall():
                # 确保呼号显示为大写
                call_sign_upper = row[0].upper()
                tree.insert("", tk.END, values=(call_sign_upper, row[1], row[2], row[3], row[4], row[5]), tags=('time_tag',))
                
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"加载历史记录失败: {str(e)}")
        
        # 布局
        tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)
        scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
        scrollbar_x.pack(side=tk.BOTTOM, fill=tk.X)
        
        # 底部按钮
        btn_frame = ttk.Frame(history_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        def delete_selected_history():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("提示", "请先选择要删除的记录")
                return
                
            if messagebox.askyesno("确认", f"确定要删除选中的{len(selected)}条记录吗？"):
                try:
                    for item in selected:
                        call_sign = tree.item(item)['values'][0]
                        self.cursor.execute(
                            'DELETE FROM call_signs WHERE call_sign = ?', 
                            (call_sign,)
                        )
                    self.conn.commit()
                    # 刷新列表
                    for item in selected:
                        tree.delete(item)
                    messagebox.showinfo("成功", "选中的历史记录已删除")
                except sqlite3.Error as e:
                    self.conn.rollback()
                    messagebox.showerror("数据库错误", f"删除记录失败: {str(e)}")
        
        ttk.Button(btn_frame, text="删除选中", command=delete_selected_history).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="关闭", command=history_window.destroy).pack(side=tk.RIGHT, padx=5)
    
    def clear_history(self):
        """清空历史记录数据"""
        print("clear_history方法被调用")
        
        # 导入simpledialog用于密码输入
        import tkinter.simpledialog
        
        # 请求用户输入密码
        password = tkinter.simpledialog.askstring("密码验证", "请输入管理员密码以清空历史记录:", show="*")
        
        # 从数据库中获取管理员密码哈希值
        try:
            # 首先尝试获取password_hash列
            try:
                self.cursor.execute("SELECT password_hash FROM admin ORDER BY id DESC LIMIT 1")
                result = self.cursor.fetchone()
                if result and result[0]:
                    # 计算输入密码的哈希值并与数据库中的进行比较
                    input_password_hash = hashlib.sha256(password.encode()).hexdigest()
                    if input_password_hash != result[0]:
                        messagebox.showerror("密码错误", "密码不正确，无法清空历史记录！")
                        return
                else:
                    # 如果password_hash为空或不存在，尝试使用password列（向后兼容）
                    self.cursor.execute("SELECT password FROM admin ORDER BY id DESC LIMIT 1")
                    result = self.cursor.fetchone()
                    if not result or password != result[0]:
                        messagebox.showerror("密码错误", "密码不正确，无法清空历史记录！")
                        return
            except sqlite3.OperationalError:
                # 如果password_hash列不存在，尝试使用password列（向后兼容）
                self.cursor.execute("SELECT password FROM admin ORDER BY id DESC LIMIT 1")
                result = self.cursor.fetchone()
                if not result or password != result[0]:
                    messagebox.showerror("密码错误", "密码不正确，无法清空历史记录！")
                    return
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"获取密码失败: {str(e)}")
            return
        
        # 密码正确，显示确认对话框
        if messagebox.askyesno("确认", "确定要清空所有历史信息吗？此操作不可恢复！"):
            try:
                print("执行DELETE FROM call_signs")
                self.cursor.execute('DELETE FROM call_signs')
                print("执行DELETE FROM roll_call_records")
                self.cursor.execute('DELETE FROM roll_call_records')
                print("执行commit")
                self.conn.commit()
                print("commit成功")
                # 清空内存中的记录
                self.records = []
                # 重置当前名次（使用起始名次）
                self.current_rank = self.start_rank
                # 清空表格
                for item in self.tree.get_children():
                    self.tree.delete(item)
                messagebox.showinfo("提示", "历史信息已清空")
            except sqlite3.Error as e:
                print(f"清空历史记录失败: {str(e)}")
                self.conn.rollback()
                messagebox.showerror("数据库错误", f"清空历史记录失败: {str(e)}")
            except Exception as e:
                print(f"发生未知错误: {str(e)}")
                messagebox.showerror("错误", f"发生未知错误: {str(e)}")
        
    def delete_selected(self):
        selected_item = self.tree.selection()
        if not selected_item:
            messagebox.showinfo("提示", "请先选择要删除的记录")
            return
        
        # 检查是否选中的是主控行
        item_values = self.tree.item(selected_item[0])['values']
        if item_values and item_values[0] == "主控":
            messagebox.showinfo("提示", "不能删除主控行")
            return
            
        try:
            # 获取选中项的索引
            tree_index = self.tree.index(selected_item[0])
            
            # 计算records列表中的对应索引（主控行在tree_index=0，不存储在records中）
            records_index = tree_index - 1
            
            # 确保索引有效
            if records_index < 0 or records_index >= len(self.records):
                raise IndexError("无效的记录索引")
            
            call_sign = self.records[records_index][1]
            confirm_time_full = self.records[records_index][8]  # 使用存储的完整时间（索引8）
            
            # 从数据和表格中删除
            del self.records[records_index]
            self.tree.delete(selected_item)
            
            # 从数据库删除
            self.cursor.execute('''
            DELETE FROM roll_call_records 
            WHERE call_sign = ? AND confirm_time = ?
            ''', (call_sign, confirm_time_full))
            
            self.conn.commit()
            
            # 更新所有记录的名次
            self.update_ranks()
            
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("数据库错误", f"删除记录失败: {str(e)}")
        except IndexError as e:
            messagebox.showerror("错误", f"删除记录失败: {str(e)}")
        
    def update_ranks(self):
        # 保存当前主控行的值
        master_values = None
        for child in self.tree.get_children():
            item = self.tree.item(child)
            if item['values'][0] == "主控":
                master_values = item['values']
                break
        
        # 清空表格
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        # 重新插入主控行（如果存在）
        if master_values:
            self.tree.insert("", 0, values=master_values, tags=('master_tag',))
        else:
            # 如果不存在，插入空的主控行
            self.tree.insert("", 0, values=("主控", "", "", "", "", "", ""), tags=('master_tag',))
        
        # 按照确认时间对记录进行排序（从早到晚）
        self.records.sort(key=lambda x: x[8])  # 索引8是完整时间
        
        # 更新内存中的记录名次
        self.current_rank = self.start_rank
        for i, record in enumerate(self.records):
            # 更新记录名次
            updated_record = (
                i + self.start_rank, 
                record[1].upper(), 
                record[2], 
                record[3], 
                record[4],  # 功率信息
                record[5],  # 接入方式
                record[6],  # 天馈参数
                record[7],  # 显示时间
                record[8],  # 完整时间（用于内部处理）
                record[9],  # 主控呼号
                record[10]  # 台网点名
            )
            self.records[i] = updated_record
            # 插入记录到表格（插入到主控行之后，保持时间顺序）
            self.tree.insert("", 1, values=(
                updated_record[0], 
                updated_record[1], 
                updated_record[2], 
                updated_record[3], 
                updated_record[4],  # 功率
                updated_record[6],  # 天馈参数
                updated_record[5],  # 接入方式
                updated_record[7]   # 确认时间
            ), tags=('time_tag',))
            
            # 更新数据库中的名次
            try:
                self.cursor.execute('''
                UPDATE roll_call_records 
                SET rank = ? 
                WHERE call_sign = ? AND confirm_time = ?
                ''', (i + self.start_rank, record[1], record[8]))
            except sqlite3.Error as e:
                self.conn.rollback()
                messagebox.showerror("数据库错误", f"更新名次失败: {str(e)}")
                return
        
        self.conn.commit()
        self.current_rank = len(self.records) + self.start_rank
        
    def set_start_rank(self):
        """设置起始名次"""
        # 创建设置对话框
        start_rank_window = tk.Toplevel(self.root)
        start_rank_window.title("设置起始名次")
        # 计算时隙选择框的位置
        timeslot_x = self.digital_network_frame.winfo_rootx() + 650
        timeslot_y = self.digital_network_frame.winfo_rooty() + 50
        
        # 设置窗口大小和位置
        start_rank_window.geometry("300x180+{}+{}".format(timeslot_x, timeslot_y))
        start_rank_window.transient(self.root)
        start_rank_window.grab_set()
        
        # 创建输入框架
        input_frame = ttk.Frame(start_rank_window, padding="20")
        input_frame.pack(fill=tk.BOTH, expand=True)
        
        # 当前起始名次显示
        ttk.Label(input_frame, text=f"当前起始名次: {self.start_rank}", font=("SimHei", 10)).pack(pady=5)
        
        # 新起始名次输入
        ttk.Label(input_frame, text="新起始名次:", font=("SimHei", 10)).pack(pady=5)
        new_start_rank_var = tk.StringVar(value=str(self.start_rank))
        start_rank_entry = ttk.Entry(input_frame, textvariable=new_start_rank_var, width=10)
        start_rank_entry.pack(pady=5)
        start_rank_entry.focus()
        start_rank_entry.select_range(0, tk.END)
        
        def apply_start_rank():
            """应用新的起始名次"""
            try:
                new_rank = int(new_start_rank_var.get().strip())
                if new_rank < 1:
                    messagebox.showerror("错误", "起始名次必须大于0")
                    return
                
                self.start_rank = new_rank
                # 更新当前名次
                self.current_rank = len(self.records) + self.start_rank
                messagebox.showinfo("成功", f"起始名次已设置为: {self.start_rank}")
                start_rank_window.destroy()
                
                # 更新所有记录的名次
                self.update_ranks()
                
            except ValueError:
                messagebox.showerror("错误", "请输入有效的数字")
        
        # 按钮框架
        button_frame = ttk.Frame(start_rank_window)
        button_frame.pack(pady=10)
        
        ttk.Button(button_frame, text="确定", command=apply_start_rank).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="取消", command=start_rank_window.destroy).pack(side=tk.LEFT, padx=5)
        
    def export_records(self):
        if not self.records:
            messagebox.showinfo("提示", "没有记录可导出")
            return
            
        # 获取台网点名，确保不为空
        station_name = self.station_name_var.get().strip()
        if not station_name:
            messagebox.showwarning("提示", "请先输入台网点名")
            self.station_name_entry.focus()
            return
            
        # 生成默认文件名：日期+台网点名（过滤非法字符）
        current_date = datetime.now().strftime("%Y%m%d")
        # 过滤文件名中的非法字符
        invalid_chars = '/\\:*?"<>|'
        clean_station_name = ''.join([c for c in station_name if c not in invalid_chars])
        default_filename = f"{current_date}_{clean_station_name}"
        
        # 询问保存路径，使用默认文件名，添加多种格式支持
        file_path = filedialog.asksaveasfilename(
            defaultextension="",
            filetypes=[
                ("PDF文件", "*.pdf"),
                ("Word文档", "*.docx"),
                ("CSV文件", "*.csv"),
                ("JPEG图片", "*.jpg"),
                ("所有文件", "*.*")
            ],
            title="保存表单",
            initialfile=default_filename
        )
        
        if not file_path:
            return
            
        try:
            # 获取文件扩展名并转为小写
            _, file_ext = os.path.splitext(file_path)
            file_ext = file_ext.lower()
            
            # 根据文件扩展名选择不同的导出方法
            if file_ext == '.pdf':
                self._export_to_pdf(file_path, station_name)
            elif file_ext == '.docx':
                self._export_to_docx(file_path, station_name)
            elif file_ext == '.csv':
                self._export_to_csv(file_path)
            elif file_ext == '.jpg':
                self._export_to_jpeg(file_path, station_name)
            else:
                # 默认使用DOCX格式
                self._export_to_docx(file_path, station_name)
            
            messagebox.showinfo("成功", f"记录已导出到:\n{file_path}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")
            
    def _export_to_pdf(self, file_path, station_name):
        """导出为PDF格式"""
        # 注册宋体字体，尝试多种可能的字体路径
        font_loaded = False
        font_names = ['SimSun', 'SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'Heiti TC']
        font_paths = ['simsun.ttc', 'C:\\Windows\\Fonts\\simsun.ttc', 'C:\\Windows\\Fonts\\simsun.ttf', 'SimSun.ttf', '/Library/Fonts/SimSun.ttf', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc']
        
        # 尝试加载宋体
        for font_name in font_names:
            for font_path in font_paths:
                try:
                    pdfmetrics.registerFont(TTFont(font_name, font_path))
                    font_loaded = True
                    break
                except:
                    continue
            if font_loaded:
                break
        
        if not font_loaded:
            # 尝试使用默认字体
            default_font = 'Helvetica'
            font_name = default_font
            messagebox.showwarning("警告", "无法加载宋体，将使用默认字体。PDF中的中文可能无法正确显示。")
        else:
            # 使用宋体
            font_name = 'SimSun'

        # 创建PDF文档
        doc = SimpleDocTemplate(file_path, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        # 修改样式以支持中文
        # 设置台网信息字体大小（14号）
        styles['Heading1'].fontName = font_name
        styles['Heading1'].fontSize = 14
        styles['Normal'].fontName = font_name
        
        # 添加标题（包含主控呼号）- 台网信息用14号宋体，整体居中
        master_call_sign = self.operator_call_sign_var.get()
        title_text = f"台网点名记录 - {station_name} (主控呼号: {master_call_sign})"
        title = Paragraph(title_text, styles['Heading1'])
        # 确保标题居中显示
        title.alignment = 1  # 0=左对齐, 1=居中, 2=右对齐
        elements.append(title)
        elements.append(Spacer(1, 12))
        
        # 添加日期（居中对齐）- 点名日期用11号字
        date_style = styles['Normal'].clone('DateStyle')
        date_style.fontSize = 11
        date_text = Paragraph(f"点名日期: {datetime.now().strftime('%Y年%m月%d日')}", date_style)
        # 设置日期居中对齐
        date_text.alignment = 1  # 0=左对齐, 1=居中, 2=右对齐
        elements.append(date_text)
        elements.append(Spacer(1, 12))
        
        # 准备表格数据
        data = [["名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时刻"]]  # 表头
        
        # 首先添加主控行（如果存在）
        master_call_sign = self.operator_call_sign_var.get().strip()
        if master_call_sign:
            # 从树形视图中获取主控行的完整信息
            master_info = None
            for item in self.tree.get_children():
                values = self.tree.item(item).get('values', [])
                if values and len(values) > 0 and values[0] == "主控":
                    master_info = values
                    break
            
            if master_info:
                master_qth = (master_info[2] if len(master_info) > 2 and master_info[2] else "")  # QTH
                master_equip = (master_info[3] if len(master_info) > 3 and master_info[3] else "")  # 设备
                master_power = (master_info[4] if len(master_info) > 4 and master_info[4] else "")  # 功率
                master_antenna = (master_info[5] if len(master_info) > 5 and master_info[5] else "")  # 天馈参数
                master_method = (master_info[6] if len(master_info) > 6 and master_info[6] else "")  # 接入方式
                master_time = (master_info[7] if len(master_info) > 7 and master_info[7] else datetime.now().strftime('%H:%M'))  # 时间
            else:
                # 如果在树形视图中找不到主控行，尝试从输入框获取（备选方案）
                master_qth = self.qth_var.get().strip() if hasattr(self, 'qth_var') else ""
                master_equip = self.equipment_var.get().strip() if hasattr(self, 'equipment_var') else ""
                master_power = self.power_var.get().strip() if hasattr(self, 'power_var') else ""
                master_antenna = self.antenna_var.get().strip() if hasattr(self, 'antenna_var') else ""
                master_method = self.access_method_var.get().strip() if hasattr(self, 'access_method_var') else ""
                master_time = datetime.now().strftime('%H:%M')
            
            # 调试信息：打印主控行数据
            print("[调试] 主控行数据:")
            print(f"  呼号: {master_call_sign}")
            print(f"  QTH: {master_qth}")
            print(f"  设备: {master_equip}")
            print(f"  功率: {master_power}")
            print(f"  天馈参数: {master_antenna}")
            print(f"  接入方式: {master_method}")
            print(f"  时间: {master_time}")
            
            data.append(["主控", master_call_sign, master_qth, master_equip, master_power, master_antenna, master_method, master_time])
        
        # 添加记录数据 - 数据结构已调整，索引5是天馈参数，索引6是接入方式
        for record in self.records:
            # record结构: (名次, 呼号, QTH, 设备, 功率, 天馈参数, 接入方式, 显示时间, 完整时间, 主控呼号, 台网)
            rank = record[0]
            call_sign = record[1]
            qth = record[2]
            equip = record[3]
            power = record[4]
            method = record[5]  # 接入方式
            antenna = record[6]  # 天馈参数
            time = record[7]     # 显示时间
            data.append([rank, call_sign.upper(), qth, equip, power, antenna, method, time])
        
        # 创建表格
        table = Table(data)

        # 设置表格宽度为页面宽度的95%
        table.width = doc.width * 0.95

        # 应用表格样式 - 所有文字用8号宋体，交替行背景色
        table_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),  # 表头背景色
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), font_name),  # 所有单元格使用宋体
            ('FONTSIZE', (0, 0), (-1, -1), 8),  # 所有文字用8号字
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ])
        
        # 如果存在主控行，为其设置特殊样式
        if master_call_sign:
            # 主控行在索引1（第二行，因为第一行是表头）
            table_style.add('BACKGROUND', (0, 1), (-1, 1), colors.lightblue)  # 天蓝色背景
            table_style.add('FONTSIZE', (0, 1), (-1, 1), 8)  # 主控行用8号字
        
        # 为数据行添加交替背景色（灰色和白色交替），从第2行开始（索引2）
        table_style.add('ROWBACKGROUNDS', (0, 2), (-1, -1), [colors.white, colors.lightgrey])
        
        table.setStyle(table_style)
        
        # 添加表格到元素列表
        elements.append(table)
        
        # 构建PDF
        doc.build(elements)
        
    def _export_to_docx(self, file_path, station_name):
        """导出为DOCX格式"""
        # 创建一个新的Word文档
        doc = docx.Document()
        
        # 添加标题
        master_call_sign = self.operator_call_sign_var.get()
        title = f"台网点名记录 - {station_name} (主控呼号: {master_call_sign})"
        doc.add_heading(title, level=1)
        
        # 添加日期
        date_text = f"点名日期: {datetime.now().strftime('%Y年%m月%d日')}"
        doc.add_paragraph(date_text)
        doc.add_paragraph()  # 添加空行
        
        # 创建表格
        # 表头
        headers = ["名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时刻"]
        
        # 创建表格，行数为记录数+1（表头），列数为表头长度
        table = doc.add_table(rows=1, cols=len(headers))
        table.style = 'Table Grid'  # 设置表格样式
        
        # 添加表头
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = header
            try:
                hdr_cells[i].paragraphs[0].runs[0].bold = True  # 设置表头文字为粗体
            except (IndexError, AttributeError):
                # 当段落没有runs时，跳过加粗，避免越界
                pass
        
        # 获取主控行信息
        master_info = None
        for item in self.tree.get_children():
            values = self.tree.item(item).get('values', [])
            if values and len(values) > 0 and values[0] == "主控":
                master_info = values
                break

        # 首先添加主控行（如果存在）
        if master_info:
            master_row_cells = table.add_row().cells
            master_row_cells[0].text = "主控"
            master_row_cells[1].text = str(master_info[1] if len(master_info) > 1 and master_info[1] else "")  # 呼号
            master_row_cells[2].text = str(master_info[2] if len(master_info) > 2 and master_info[2] else "")  # QTH
            master_row_cells[3].text = str(master_info[3] if len(master_info) > 3 and master_info[3] else "")  # 设备
            master_row_cells[4].text = str(master_info[4] if len(master_info) > 4 and master_info[4] else "")  # 功率
            master_row_cells[5].text = str(master_info[5] if len(master_info) > 5 and master_info[5] else "")  # 天馈参数
            master_row_cells[6].text = str(master_info[6] if len(master_info) > 6 and master_info[6] else "")  # 接入方式
            master_row_cells[7].text = str(master_info[7] if len(master_info) > 7 and master_info[7] else datetime.now().strftime('%H:%M'))  # 时间
            
            # 设置主控行样式：加粗字体和天蓝色背景
            for cell in master_row_cells:
                # 设置背景色为天蓝色
                cell._tc.get_or_add_tcPr().append(
                    docx.oxml.parse_xml(r'<w:shd {} w:fill="87CEEB"/>'.format(
                        docx.oxml.ns.nsdecls('w')))
                )
                # 设置字体为粗体
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
        
        # 添加数据
        for record in self.records:
            # record结构: (名次, 呼号, QTH, 设备, 功率, 接入方式, 天馈参数, 显示时间, 完整时间, 主控呼号, 台网)
            rank = record[0] if len(record) > 0 else ""
            call_sign = record[1] if len(record) > 1 else ""
            qth = record[2] if len(record) > 2 else ""
            equip = record[3] if len(record) > 3 else ""
            power = record[4] if len(record) > 4 else ""
            method = record[5] if len(record) > 5 else ""  # 接入方式
            antenna = record[6] if len(record) > 6 else ""  # 天馈参数
            time = record[7] if len(record) > 7 else ""     # 显示时间
            
            row_cells = table.add_row().cells
            row_cells[0].text = str(rank)
            row_cells[1].text = call_sign.upper()
            row_cells[2].text = qth
            row_cells[3].text = equip
            row_cells[4].text = str(power)
            row_cells[5].text = antenna  # 交换位置：天馈参数
            row_cells[6].text = method   # 交换位置：接入方式
            row_cells[7].text = time
        
        # 保存文档
        doc.save(file_path)
        
    def _export_to_csv(self, file_path):
        """导出为CSV格式"""
        # 表头
        headers = ["名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时刻"]
        
        # 写入CSV文件
        with open(file_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)
            # 写入表头
            writer.writerow(headers)
            
            # 获取主控行信息
            master_info = None
            for item in self.tree.get_children():
                values = self.tree.item(item).get('values', [])
                if values and len(values) > 0 and values[0] == "主控":
                    master_info = values
                    break

            # 写入主控行（如果存在）
            if master_info:
                writer.writerow([
                    "主控",
                    (master_info[1] if len(master_info) > 1 and master_info[1] else ""),  # 呼号
                    (master_info[2] if len(master_info) > 2 and master_info[2] else ""),  # QTH
                    (master_info[3] if len(master_info) > 3 and master_info[3] else ""),  # 设备
                    (master_info[4] if len(master_info) > 4 and master_info[4] else ""),  # 功率
                    (master_info[5] if len(master_info) > 5 and master_info[5] else ""),  # 天馈参数
                    (master_info[6] if len(master_info) > 6 and master_info[6] else ""),  # 接入方式
                    (master_info[7] if len(master_info) > 7 and master_info[7] else datetime.now().strftime('%H:%M'))  # 时间
                ])
            
            # 写入数据
            for record in self.records:
                # record结构: (名次, 呼号, QTH, 设备, 功率, 接入方式, 天馈参数, 显示时间, 完整时间, 主控呼号, 台网)
                rank = record[0] if len(record) > 0 else ""
                call_sign = record[1] if len(record) > 1 else ""
                qth = record[2] if len(record) > 2 else ""
                equip = record[3] if len(record) > 3 else ""
                power = record[4] if len(record) > 4 else ""
                method = record[5] if len(record) > 5 else ""  # 接入方式
                antenna = record[6] if len(record) > 6 else ""  # 天馈参数
                time = record[7] if len(record) > 7 else ""     # 显示时间
                writer.writerow([rank, call_sign.upper(), qth, equip, power, antenna, method, time])
                
    def _export_to_jpeg(self, file_path, station_name):
        """导出为JPEG格式"""
        # 设置图片尺寸和边距
        width = 1200
        height = 800
        margin = 50
        
        # 创建一个白色背景的图片
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)
        
        # 尝试加载中文字体
        font = None
        try:
            # 尝试使用系统中的中文字体
            font_paths = ['C:\\Windows\\Fonts\\simhei.ttf', '/Library/Fonts/SimHei.ttf', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc']
            for font_path in font_paths:
                if os.path.exists(font_path):
                    font = ImageFont.truetype(font_path, 20)
                    break
            
            if font is None:
                # 如果找不到中文字体，使用默认字体
                font = ImageFont.load_default()
        except:
            # 发生异常时使用默认字体
            font = ImageFont.load_default()
        
        # 添加标题
        master_call_sign = self.operator_call_sign_var.get()
        title = f"台网点名记录 - {station_name} (主控呼号: {master_call_sign})"
        # 计算标题居中的位置
        title_width = draw.textlength(title, font=font)
        draw.text(((width - title_width) // 2, margin), title, fill='black', font=font)
        
        # 添加日期
        date_text = f"点名日期: {datetime.now().strftime('%Y年%m月%d日')}"
        date_width = draw.textlength(date_text, font=font)
        draw.text(((width - date_width) // 2, margin + 40), date_text, fill='black', font=font)
        
        # 设置表格参数
        table_top = margin + 100
        # 表头
        headers = ["名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时刻"]
        # 列宽
        col_widths = [60, 120, 150, 180, 80, 120, 120, 150]
        
        # 绘制表头
        current_x = margin
        for i, header in enumerate(headers):
            # 绘制表头背景
            draw.rectangle([current_x, table_top, current_x + col_widths[i], table_top + 30], fill='lightgrey')
            # 绘制表头文字
            header_width = draw.textlength(header, font=font)
            draw.text((current_x + (col_widths[i] - header_width) // 2, table_top + 5), header, fill='black', font=font)
            current_x += col_widths[i]
        
        # 绘制数据行
        current_y = table_top + 30

        # 获取主控行信息
        master_info = None
        for item in self.tree.get_children():
            values = self.tree.item(item).get('values', [])
            if values and len(values) > 0 and values[0] == "主控":
                master_info = values
                break
        
        # 首先绘制主控行
        if master_info:
            current_x = margin
            # 主控行数据：主控, 呼号, QTH, 设备, 功率, 天馈参数, 接入方式, 时间
            master_row = [
                "主控",
                (master_info[1] if len(master_info) > 1 and master_info[1] else ""),
                (master_info[2] if len(master_info) > 2 and master_info[2] else ""),
                (master_info[3] if len(master_info) > 3 and master_info[3] else ""),
                (master_info[4] if len(master_info) > 4 and master_info[4] else ""),
                (master_info[5] if len(master_info) > 5 and master_info[5] else ""),
                (master_info[6] if len(master_info) > 6 and master_info[6] else ""),
                (master_info[7] if len(master_info) > 7 and master_info[7] else "")
            ]
            
            # 绘制主控行背景（使用特殊颜色）
            draw.rectangle([margin, current_y, width - margin, current_y + 30], fill='lightblue')
            
            # 绘制主控行数据
            for i, cell_data in enumerate(master_row):
                # 绘制单元格边框
                draw.rectangle([current_x, current_y, current_x + col_widths[i], current_y + 30], outline='black')
                # 绘制单元格文字
                cell_text = str(cell_data)
                text_width = draw.textlength(cell_text, font=font)
                draw.text((current_x + (col_widths[i] - text_width) // 2, current_y + 5), cell_text, fill='black', font=font)
                current_x += col_widths[i]
            current_y += 30

        # 绘制其他记录行
        for record in self.records:
            # record结构: (名次, 呼号, QTH, 设备, 功率, 接入方式, 天馈参数, 显示时间, 完整时间, 主控呼号, 台网)
            rank = record[0] if len(record) > 0 else ""
            call_sign = record[1] if len(record) > 1 else ""
            qth = record[2] if len(record) > 2 else ""
            equip = record[3] if len(record) > 3 else ""
            power = record[4] if len(record) > 4 else ""
            method = record[5] if len(record) > 5 else ""  # 接入方式
            antenna = record[6] if len(record) > 6 else ""  # 天馈参数
            time = record[7] if len(record) > 7 else ""     # 显示时间
            
            current_x = margin
            row_data = [str(rank), call_sign.upper(), qth, equip, str(power), antenna, method, time]
            
            # 绘制行背景
            if (current_y - table_top) // 30 % 2 == 1:  # 隔行变色
                draw.rectangle([margin, current_y, width - margin, current_y + 30], fill='lightyellow')
            
            # 绘制行数据
            for i, cell_data in enumerate(row_data):
                # 绘制单元格边框
                draw.rectangle([current_x, current_y, current_x + col_widths[i], current_y + 30], outline='black')
                # 绘制单元格文字（需要考虑文字换行）
                cell_width = col_widths[i] - 10  # 减去边距
                words = cell_data.split(' ')
                lines = []
                current_line = ''
                
                for word in words:
                    test_line = current_line + word + ' '
                    if draw.textlength(test_line, font=font) <= cell_width:
                        current_line = test_line
                    else:
                        if current_line:
                            lines.append(current_line)
                        current_line = word + ' '
                if current_line:
                    lines.append(current_line)
                
                # 计算文字居中的位置
                line_height = 20  # 估计的行高
                total_text_height = len(lines) * line_height
                start_y = current_y + (30 - total_text_height) // 2
                
                for j, line in enumerate(lines):
                    draw.text((current_x + 5, start_y + j * line_height), line, fill='black', font=font)
                
                current_x += col_widths[i]
            
            current_y += 30
            
            # 如果超出图片高度，停止绘制
            if current_y + 30 > height - margin:
                break
        
        # 保存图片
        image.save(file_path, 'JPEG')
    
    def search_roll_call_records(self):
        """历史点名记录查询功能"""
        search_window = tk.Toplevel(self.root)
        search_window.title("历史点名记录查询")
        search_window.geometry("1100x600")
        search_window.transient(self.root)
        search_window.grab_set()
        
        # 创建查询条件框架
        criteria_frame = ttk.LabelFrame(search_window, text="查询条件", padding="10")
        criteria_frame.pack(fill=tk.X, pady=5, padx=10)
        
        # 日期范围选择
        ttk.Label(criteria_frame, text="日期范围:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        
        # 开始日期
        ttk.Label(criteria_frame, text="开始:").grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)
        self.start_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.start_date_entry = ttk.Entry(criteria_frame, textvariable=self.start_date_var, width=15)
        self.start_date_entry.grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        
        # 结束日期
        ttk.Label(criteria_frame, text="结束:").grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)
        self.end_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.end_date_entry = ttk.Entry(criteria_frame, textvariable=self.end_date_var, width=15)
        self.end_date_entry.grid(row=0, column=4, padx=5, pady=5, sticky=tk.W)
        
        # 台网或主控查询
        ttk.Label(criteria_frame, text="台网或主控:").grid(row=0, column=5, padx=5, pady=5, sticky=tk.W)
        self.call_sign_search_var = tk.StringVar()
        self.call_sign_search_entry = ttk.Entry(criteria_frame, textvariable=self.call_sign_search_var, width=15)
        self.call_sign_search_entry.grid(row=0, column=6, padx=5, pady=5, sticky=tk.W)
        ttk.Label(criteria_frame, text="(支持模糊查询)").grid(row=0, column=7, padx=5, pady=5, sticky=tk.W)
        
        # 查询按钮
        self.search_btn = ttk.Button(criteria_frame, text="查询", command=lambda: self.perform_search(tree))
        self.search_btn.grid(row=0, column=8, padx=15, pady=5, sticky=tk.W)
        
        # 创建结果表格
        result_frame = ttk.LabelFrame(search_window, text="查询结果", padding="10")
        result_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=10)
        
        # 表格列定义
        columns = ("序号", "名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "主控呼号", "台网", "确认时间")
        tree = ttk.Treeview(result_frame, columns=columns, show="headings")
        
        # 设置列宽和标题
        column_widths = [60, 60, 120, 150, 120, 80, 120, 120, 120, 150, 180]
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=column_widths[i], anchor=tk.CENTER)
        
        # 添加滚动条
        scrollbar_y = ttk.Scrollbar(result_frame, orient=tk.VERTICAL, command=tree.yview)
        scrollbar_x = ttk.Scrollbar(result_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscroll=scrollbar_y.set, xscroll=scrollbar_x.set)
        
        # 布局
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar_y.grid(row=0, column=1, sticky=tk.NS)
        scrollbar_x.grid(row=1, column=0, sticky=tk.EW)
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        
        # 底部按钮
        btn_frame = ttk.Frame(search_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        # 导出按钮
        export_btn = ttk.Button(btn_frame, text="导出查询记录", command=lambda: self.export_search_results(tree))
        export_btn.pack(side=tk.LEFT, padx=5)
        
        # 关闭按钮
        close_btn = ttk.Button(btn_frame, text="关闭", command=search_window.destroy)
        close_btn.pack(side=tk.RIGHT, padx=5)
        
        # 执行初始查询
        self.perform_search(tree)
    
    def perform_search(self, tree):
        """执行历史点名记录查询"""
        # 清空表格
        for item in tree.get_children():
            tree.delete(item)
        
        start_date = self.start_date_var.get().strip()
        end_date = self.end_date_var.get().strip()
        call_sign = self.call_sign_search_var.get().strip().upper()
        
        # 验证日期格式
        date_pattern = r'^\d{4}-\d{2}-\d{2}$'
        if not re.match(date_pattern, start_date) or not re.match(date_pattern, end_date):
            messagebox.showerror("错误", "日期格式不正确，请使用YYYY-MM-DD格式")
            return
        
        # 确保结束日期不早于开始日期
        if start_date > end_date:
            messagebox.showerror("错误", "结束日期不能早于开始日期")
            return
        
        try:
            # 构建查询条件，使用JOIN连接call_signs表获取主控和台网信息，添加天馈参数
            query_parts = ["SELECT rc.id, rc.rank, rc.call_sign, rc.qth, rc.equipment, rc.power, rc.antenna, rc.access_method, cs.main_control, cs.station_network, rc.confirm_time FROM roll_call_records rc LEFT JOIN call_signs cs ON rc.call_sign = cs.call_sign WHERE 1=1"]
            params = []
            
            # 添加日期范围条件
            query_parts.append("AND DATE(rc.confirm_time) BETWEEN ? AND ?")
            params.extend([start_date, end_date])
            
            # 添加台网或主控条件（如果有）
            if call_sign:
                query_parts.append("AND (rc.call_sign LIKE ? OR cs.main_control LIKE ? OR cs.station_network LIKE ?)")
                params.extend([f"%{call_sign}%", f"%{call_sign}%", f"%{call_sign}%"])
            
            # 添加排序
            query_parts.append("ORDER BY rc.confirm_time DESC, rc.rank ASC")
            
            # 执行查询
            self.cursor.execute(' '.join(query_parts), params)
            results = self.cursor.fetchall()
            
            # 填充表格，包含主控呼号和台网字段
            for i, row in enumerate(results):
                # 如果主控呼号、台网或天馈参数为空，显示空字符串
                main_control = row[8] if row[8] is not None else ""
                station_network = row[9] if row[9] is not None else ""
                antenna = row[6] if row[6] is not None else ""
                # 参数顺序已调整，确保天馈参数在前，接入方式在后，与表格列顺序一致
                tree.insert("", tk.END, values=(i+1, row[1], row[2].upper(), row[3], row[4], row[5], antenna, row[7], main_control, station_network, row[10]))
                
            # 默认全选所有记录
            if len(results) > 0:
                tree.selection_set(tree.get_children())
                
            # 显示查询结果数量
            messagebox.showinfo("查询完成", f"共找到 {len(results)} 条记录")
        except sqlite3.Error as e:
            messagebox.showerror("数据库错误", f"查询失败: {str(e)}")
    
    def export_search_results(self, tree):
        """导出查询结果"""
        selected_items = tree.selection()
        if not selected_items:
            messagebox.showinfo("提示", "请先选择要导出的记录")
            return
        
        # 获取选中记录
        selected_records = []
        main_control = ""
        station_network = ""
        
        for item in selected_items:
            values = tree.item(item)['values']
            selected_records.append(values)
            
            # 提取主控呼号和台网信息（取第一条记录的值）
            if not main_control and len(values) > 8:
                main_control = values[8] if values[8] is not None else ""
            if not station_network and len(values) > 9:
                station_network = values[9] if values[9] is not None else ""
        
        # 生成默认文件名（增加时间字段）
        current_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_filename = f"{current_datetime}_查询结果"
        
        # 询问保存路径，添加多种格式支持
        file_path = filedialog.asksaveasfilename(
            defaultextension="",
            filetypes=[
                ("Word文档", "*.docx"),
                ("PDF文件", "*.pdf"),
                ("CSV文件", "*.csv"),
                ("JPEG图片", "*.jpg"),
                ("所有文件", "*.*")
            ],
            title="保存查询结果",
            initialfile=default_filename
        )
        
        if not file_path:
            return
        
        try:
            # 获取文件扩展名并转为小写
            _, file_ext = os.path.splitext(file_path)
            file_ext = file_ext.lower()
            
            # 根据文件扩展名选择不同的导出方法
            if file_ext == '.pdf':
                self._export_search_to_pdf(file_path, selected_records, main_control, station_network)
            elif file_ext == '.docx':
                self._export_search_to_docx(file_path, selected_records, main_control, station_network)
            elif file_ext == '.csv':
                self._export_search_to_csv(file_path, selected_records)
            elif file_ext == '.jpg':
                self._export_search_to_jpeg(file_path, selected_records, main_control, station_network)
            else:
                # 默认使用DOCX格式
                self._export_search_to_docx(file_path, selected_records, main_control, station_network)
            
            messagebox.showinfo("成功", f"查询结果已导出到:\n{file_path}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")
            
    def _export_search_to_pdf(self, file_path, selected_records, main_control, station_network):
        """导出查询结果为PDF格式"""
        # 注册宋体字体，尝试多种可能的字体路径
        font_loaded = False
        font_names = ['SimSun', 'SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'Heiti TC']
        font_paths = ['simsun.ttc', 'C:\\Windows\\Fonts\\simsun.ttc', 'C:\\Windows\\Fonts\\simsun.ttf', 'SimSun.ttf', '/Library/Fonts/SimSun.ttf', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc']
        
        # 尝试加载宋体
        for font_name in font_names:
            for font_path in font_paths:
                try:
                    pdfmetrics.registerFont(TTFont(font_name, font_path))
                    font_loaded = True
                    break
                except:
                    continue
            if font_loaded:
                break
        
        if not font_loaded:
            # 尝试使用默认字体
            font_name = 'Helvetica'
            messagebox.showwarning("警告", "无法加载宋体，将使用默认字体。PDF中的中文可能无法正确显示。")
        else:
            # 使用宋体
            font_name = 'SimSun'
        
        # 创建PDF文档
        doc = SimpleDocTemplate(file_path, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        # 修改样式以支持中文
        styles['Heading1'].fontName = font_name
        styles['Heading1'].fontSize = 12
        styles['Normal'].fontName = font_name
        
        # 添加标题
        title_text = f"<b>     历史点名记录查询结果 ({self.start_date_var.get()} 至 {self.end_date_var.get()})</b>"
        title = Paragraph(title_text, styles['Heading1'])
        title.alignment = 1  # 居中
        elements.append(title)
        elements.append(Spacer(1, 12))
        
        # 添加导出日期
        date_text = Paragraph(f"导出日期: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}", styles['Normal'])
        date_text.alignment = 1  # 居中
        elements.append(date_text)
        elements.append(Spacer(1, 12))
        
        # 添加主控呼号和台网信息到标题区域
        if main_control or station_network:
            info_text = """<b>查询条件信息：</b><br/>%s%s""" % (
                f"主控呼号: {main_control}<br/>" if main_control else "",
                f"台网: {station_network}" if station_network else ""
            )
            info = Paragraph(info_text, styles['Normal'])
            info.alignment = 1  # 居中
            elements.append(info)
            elements.append(Spacer(1, 12))
        
        # 准备表格数据 - 移除表格中的主控呼号和台网字段，并格式化确认时间到秒级
        data = [["序号", "名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时间"]]
        for record in selected_records:
            # 移除主控呼号和台网字段（索引8和9），保留确认时间（索引10）
            if len(record) > 10:
                # 获取确认时间并格式化到秒级
                confirm_time = record[10]
                # 处理字符串类型的时间（如果包含毫秒）
                if isinstance(confirm_time, str) and '.' in confirm_time:
                    # 只保留秒级精度
                    confirm_time = confirm_time.split('.')[0]
                # 数据结构已调整，索引5是天馈参数，索引6是接入方式
                simplified_record = [record[0], record[1], record[2], record[3], record[4], record[5], record[6], confirm_time]
            else:
                simplified_record = record
            data.append(simplified_record)
        
        # 创建表格
        table = Table(data)
        table.width = doc.width * 0.95
        
        # 应用表格样式
        table_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), font_name),
            ('FONTNAME', (0, 1), (-1, -1), font_name),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ])
        table.setStyle(table_style)
        
        # 添加表格到元素列表
        elements.append(table)
        
        # 构建PDF
        doc.build(elements)
        
    def _export_search_to_docx(self, file_path, selected_records, main_control, station_network):
        """导出查询结果为DOCX格式"""
        # 创建一个新的Word文档
        doc = docx.Document()
        
        # 设置中文字体
        from docx.shared import Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        
        # 添加标题
        title = f"历史点名记录查询结果 ({self.start_date_var.get()} 至 {self.end_date_var.get()})"
        title_para = doc.add_heading(title, level=1)
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # 添加导出日期 - 宋体11号，整体居中
        date_text = f"导出日期: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}"
        date_para = doc.add_paragraph(date_text)
        date_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        date_run = date_para.runs[0]
        date_run.font.name = '宋体'
        date_run.font.size = Pt(11)
        
        # 添加主控呼号和台网信息 - 宋体14号，整体居中
        if main_control or station_network:
            info_text = "查询条件信息：\n"
            if main_control:
                info_text += f"主控呼号: {main_control}\n"
            if station_network:
                info_text += f"台网: {station_network}\n"
            info_para = doc.add_paragraph(info_text)
            info_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            info_run = info_para.runs[0]
            info_run.font.name = '宋体'
            info_run.font.size = Pt(14)
        
        doc.add_paragraph()  # 添加空行
        
        # 创建表格
        # 表头
        headers = ["序号", "名次", "呼号", "QTH", "设备", "功率值", "天馈参数", "接入方式", "确认时间"]
        
        # 创建表格，行数为记录数+1（表头），列数为表头长度
        table = doc.add_table(rows=1, cols=len(headers))
        table.style = 'Table Grid'  # 设置表格样式
        
        # 添加表头 - 8号宋体加粗
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = header
            hdr_para = hdr_cells[i].paragraphs[0]
            hdr_run = hdr_para.runs[0]
            hdr_run.font.name = '宋体'
            hdr_run.font.size = Pt(8)
            hdr_run.bold = True  # 设置表头文字为粗体
        
        # 添加数据
        for record in selected_records:
            # 移除主控呼号和台网字段（索引8和9），保留确认时间（索引10）
            if len(record) > 10:
                # 获取确认时间并格式化到秒级
                confirm_time = record[10]
                # 处理字符串类型的时间（如果包含毫秒）
                if isinstance(confirm_time, str) and '.' in confirm_time:
                    # 只保留秒级精度
                    confirm_time = confirm_time.split('.')[0]
                # 数据结构已调整，索引5是天馈参数，索引6是接入方式
                simplified_record = [record[0], record[1], record[2], record[3], record[4], record[5], record[6], confirm_time]
            else:
                simplified_record = record
            
            # 添加数据行
            row_cells = table.add_row().cells
            for i, cell_data in enumerate(simplified_record):
                if i < len(row_cells):
                    row_cells[i].text = str(cell_data) if cell_data is not None else ""
                    # 设置单元格字体为8号宋体
                    cell_para = row_cells[i].paragraphs[0]
                    cell_run = cell_para.runs[0]
                    cell_run.font.name = '宋体'
                    cell_run.font.size = Pt(8)
                    
                    # 如果是主控行（名次为1），设置整行加粗
                    if i == 1 and cell_data == 1:  # 名次列为1表示主控
                        cell_run.bold = True
        
        # 保存文档
        doc.save(file_path)
        
    def _export_search_to_csv(self, file_path, selected_records):
        """导出查询结果为CSV格式"""
        # 表头
        headers = ["序号", "名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时间"]
        
        # 写入CSV文件
        with open(file_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)
            # 写入表头
            writer.writerow(headers)
            # 写入数据
            for record in selected_records:
                # 移除主控呼号和台网字段（索引8和9），保留确认时间（索引10）
                if len(record) > 10:
                    # 获取确认时间并格式化到秒级
                    confirm_time = record[10]
                    # 处理字符串类型的时间（如果包含毫秒）
                    if isinstance(confirm_time, str) and '.' in confirm_time:
                        # 只保留秒级精度
                        confirm_time = confirm_time.split('.')[0]
                    # 数据结构已调整，索引5是天馈参数，索引6是接入方式
                    simplified_record = [record[0], record[1], record[2], record[3], record[4], record[5], record[6], confirm_time]
                else:
                    simplified_record = record
                
                # 写入数据行
                writer.writerow([str(cell) if cell is not None else "" for cell in simplified_record])
                
    def _export_search_to_jpeg(self, file_path, selected_records, main_control, station_network):
        """导出查询结果为JPEG格式"""
        # 设置图片尺寸和边距
        width = 1400
        height = 900
        margin = 50
        
        # 创建一个白色背景的图片
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)
        
        # 尝试加载中文字体
        font = None
        try:
            # 尝试使用系统中的中文字体
            font_paths = ['C:\\Windows\\Fonts\\simhei.ttf', '/Library/Fonts/SimHei.ttf', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc']
            for font_path in font_paths:
                if os.path.exists(font_path):
                    font = ImageFont.truetype(font_path, 20)
                    break
            
            if font is None:
                # 如果找不到中文字体，使用默认字体
                font = ImageFont.load_default()
        except:
            # 发生异常时使用默认字体
            font = ImageFont.load_default()
        
        # 添加标题
        title = f"历史点名记录查询结果 ({self.start_date_var.get()} 至 {self.end_date_var.get()})"
        # 计算标题居中的位置
        title_width = draw.textlength(title, font=font)
        draw.text(((width - title_width) // 2, margin), title, fill='black', font=font)
        
        # 添加导出日期
        date_text = f"导出日期: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}"
        date_width = draw.textlength(date_text, font=font)
        draw.text(((width - date_width) // 2, margin + 40), date_text, fill='black', font=font)
        
        # 添加主控呼号和台网信息
        info_y = margin + 80
        if main_control:
            main_control_text = f"主控呼号: {main_control}"
            main_control_width = draw.textlength(main_control_text, font=font)
            draw.text(((width - main_control_width) // 2, info_y), main_control_text, fill='black', font=font)
            info_y += 30
        if station_network:
            station_network_text = f"台网: {station_network}"
            station_network_width = draw.textlength(station_network_text, font=font)
            draw.text(((width - station_network_width) // 2, info_y), station_network_text, fill='black', font=font)
            info_y += 30
        
        # 设置表格参数
        table_top = info_y + 20
        # 表头
        headers = ["序号", "名次", "呼号", "QTH", "设备", "功率(W)", "天馈参数", "接入方式", "确认时间"]
        # 列宽
        col_widths = [60, 60, 120, 150, 120, 80, 120, 120, 150]
        
        # 绘制表头
        current_x = margin
        for i, header in enumerate(headers):
            # 绘制表头背景
            draw.rectangle([current_x, table_top, current_x + col_widths[i], table_top + 30], fill='lightgrey')
            # 绘制表头文字
            header_width = draw.textlength(header, font=font)
            draw.text((current_x + (col_widths[i] - header_width) // 2, table_top + 5), header, fill='black', font=font)
            current_x += col_widths[i]
        
        # 绘制数据行
        current_y = table_top + 30
        for record in selected_records:
            # 处理记录数据
            if len(record) > 10:
                # 获取确认时间并格式化到秒级
                confirm_time = record[10]
                # 处理字符串类型的时间（如果包含毫秒）
                if isinstance(confirm_time, str) and '.' in confirm_time:
                    # 只保留秒级精度
                    confirm_time = confirm_time.split('.')[0]
                # 数据结构已调整，索引5是天馈参数，索引6是接入方式
                simplified_record = [record[0], record[1], record[2], record[3], record[4], record[5], record[6], confirm_time]
            else:
                simplified_record = record
            
            current_x = margin
            
            # 绘制行背景
            if (current_y - table_top) // 30 % 2 == 1:  # 隔行变色
                draw.rectangle([margin, current_y, width - margin, current_y + 30], fill='lightyellow')
            
            # 绘制行数据
            for i, cell_data in enumerate(simplified_record):
                if i >= len(col_widths):  # 防止索引越界
                    continue
                
                # 绘制单元格边框
                draw.rectangle([current_x, current_y, current_x + col_widths[i], current_y + 30], outline='black')
                
                # 处理单元格数据
                cell_text = str(cell_data) if cell_data is not None else ""
                
                # 绘制单元格文字（需要考虑文字换行）
                cell_width = col_widths[i] - 10  # 减去边距
                words = cell_text.split(' ')
                lines = []
                current_line = ''
                
                for word in words:
                    test_line = current_line + word + ' '
                    if draw.textlength(test_line, font=font) <= cell_width:
                        current_line = test_line
                    else:
                        if current_line:
                            lines.append(current_line)
                        current_line = word + ' '
                if current_line:
                    lines.append(current_line)
                
                # 计算文字居中的位置
                line_height = 20  # 估计的行高
                total_text_height = len(lines) * line_height
                start_y = current_y + (30 - total_text_height) // 2
                
                for j, line in enumerate(lines):
                    draw.text((current_x + 5, start_y + j * line_height), line, fill='black', font=font)
                
                current_x += col_widths[i]
            
            current_y += 30
            
            # 如果超出图片高度，停止绘制
            if current_y + 30 > height - margin:
                break
        
        # 保存图片
        image.save(file_path, 'JPEG')
    
    def create_menu(self):
        """创建应用程序菜单"""
        # 创建菜单栏
        menubar = tk.Menu(self.root)
        
        # 创建设置菜单
        settings_menu = tk.Menu(menubar, tearoff=0)
        
        # P2P 同步子菜单
        p2p_menu = tk.Menu(settings_menu, tearoff=0)
        p2p_menu.add_command(label="P2P 同步设置", command=self.show_p2p_config_dialog)
        p2p_menu.add_command(label="立即同步", command=self.p2p_sync_now)
        settings_menu.add_cascade(label="P2P 同步", menu=p2p_menu)
        
        # 添加设置菜单到菜单栏
        menubar.add_cascade(label="设置", menu=settings_menu)
        
        # 帮助菜单
        menu_help = tk.Menu(menubar, tearoff=0)
        menu_help.add_command(label="检查更新", command=self.manual_check_update)
        menubar.add_cascade(label="帮助", menu=menu_help)
        
        # 设置窗口的菜单栏
        self.root.config(menu=menubar)
        
    
    # ========== P2P 同步 ==========
    def get_p2p_config_path(self):
        """P2P 配置文件路径（优先 exe 同目录，但必须包含 [P2P] 配置）"""
        path = os.path.join(APP_DIR, "app_config.ini")
        if os.path.exists(path):
            cfg = configparser.ConfigParser()
            try:
                cfg.read(path, encoding="utf-8")
            except Exception:
                cfg = None
            if cfg is not None and "P2P" in cfg:
                return path
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_config.ini")
    
    def init_p2p_sync(self):
        """按 app_config.ini 的 [P2P] 配置启动同步引擎"""
        if P2PSyncEngine is None:
            print("P2P 同步模块不可用，已跳过")
            return
        try:
            config = configparser.ConfigParser()
            config_path = self.get_p2p_config_path()
            if os.path.exists(config_path):
                config.read(config_path, encoding="utf-8")
            if "P2P" not in config or not config["P2P"].getboolean("enabled", fallback=False):
                return
            db_path = os.path.join(APP_DIR, "call_sign_history.db")
            self.p2p_engine = P2PSyncEngine(db_path, config_path, logger=print,
                                            on_status=self.on_p2p_status)
            self.p2p_engine.start()
        except Exception as exc:
            self.p2p_engine = None
            print(f"P2P 同步引擎启动失败: {exc}")
    
    def stop_p2p_sync(self):
        """停止 P2P 同步引擎"""
        if getattr(self, "p2p_engine", None):
            try:
                self.p2p_engine.stop()
            except Exception as exc:
                print(f"P2P 同步引擎停止失败: {exc}")
            self.p2p_engine = None
        self.stop_library_sync()
        self.p2p_status = {}
        self._update_p2p_status_light()
    
    def start_library_sync(self, operator_call_sign):
        """把主控呼号库（<主控呼号>.db）接入中继汇聚同步；换主控呼号时自动切换"""
        call_sign = (operator_call_sign or "").strip()
        if not call_sign:
            return
        if self.master_lib_sync and self.master_lib_sync_target == call_sign:
            return
        self.stop_library_sync()
        try:
            from master_library_sync import MasterLibrarySync
        except ImportError:
            print("呼号库汇聚模块不可用，已跳过")
            return
        try:
            db_path = os.path.join(APP_DIR, "%s.db" % call_sign)
            self.master_lib_sync = MasterLibrarySync(
                db_path, self.get_p2p_config_path(), logger=print, interval=300,
                mirror_db=getattr(self, "history_db_path", ""), callsign=call_sign)
            self.master_lib_sync.start()
            self.master_lib_sync_target = call_sign
        except Exception as exc:
            self.master_lib_sync = None
            self.master_lib_sync_target = ""
            print(f"呼号库汇聚同步启动失败: {exc}")
    
    def stop_library_sync(self):
        """停止呼号库汇聚同步"""
        if getattr(self, "master_lib_sync", None):
            try:
                self.master_lib_sync.stop()
            except Exception as exc:
                print(f"呼号库汇聚同步停止失败: {exc}")
            self.master_lib_sync = None
        self.master_lib_sync_target = ""

    # ---------- 手机点名（中继） ----------
    def _mobile_p2p_settings(self):
        """读取中继地址与可选访问令牌"""
        cfg = configparser.ConfigParser()
        try:
            cfg.read(self.get_p2p_config_path(), encoding="utf-8")
        except Exception:
            return "", ""
        if not cfg.has_section("P2P") or not cfg.getboolean("P2P", "enabled", fallback=False):
            return "", ""
        return (cfg.get("P2P", "relay_url", fallback="").strip(),
                cfg.get("P2P", "relay_token", fallback="").strip())

    def _mobile_api(self, method, path, params=None, payload=None, timeout=5):
        relay_url, token = self._mobile_p2p_settings()
        if not relay_url:
            raise RuntimeError("P2P 未启用")
        headers = {"X-Relay-Token": token} if token else {}
        resp = requests.request(method, relay_url.rstrip("/") + path,
                                params=params, json=payload, headers=headers,
                                timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _mobile_session_conn(self, operator):
        conn = sqlite3.connect(os.path.join(APP_DIR, "%s.db" % operator), timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS mobile_session ("
                     "batch_id TEXT PRIMARY KEY, last_id INTEGER DEFAULT 0)")
        return conn

    def start_mobile_session(self, operator, station):
        """登录确认后在中继开启手机点名批次，并轮询手机提交的记录"""
        operator = (operator or "").strip()
        if not operator:
            return
        try:
            data = self._mobile_api("POST", "/api/mobile/batch",
                                    payload={"operator": operator, "name": station})
        except Exception as exc:
            print(f"手机点名批次创建失败: {exc}")
            return
        batch_id = data.get("batch_id") or ""
        if not batch_id:
            return
        if self.mobile_batch_id and self.mobile_batch_id != batch_id:
            try:
                self._mobile_api("POST", "/api/mobile/batch/close",
                                 payload={"batch_id": self.mobile_batch_id})
            except Exception:
                pass
        self.mobile_batch_id = batch_id
        try:
            conn = self._mobile_session_conn(operator)
            row = conn.execute("SELECT last_id FROM mobile_session WHERE batch_id = ?",
                               (batch_id,)).fetchone()
            self.mobile_last_id = int(row[0]) if row else 0
            conn.close()
        except sqlite3.Error as exc:
            print(f"读取手机点名游标失败: {exc}")
            self.mobile_last_id = 0
        if not self.mobile_polling:
            self.mobile_polling = True
            self.root.after(self.mobile_poll_interval, self._poll_mobile_records)
        print(f"手机点名已开启: 批次 {batch_id}")

    def _poll_mobile_records(self):
        """定时从中继拉取手机提交的点名记录"""
        if not self.mobile_polling:
            return
        if self.mobile_batch_id:
            try:
                data = self._mobile_api("GET", "/api/mobile/records",
                                        params={"batch_id": self.mobile_batch_id,
                                                "since": self.mobile_last_id})
                rows = data.get("records") or []
                if rows:
                    self._apply_mobile_records(rows)
            except Exception as exc:
                print(f"手机点名拉取失败: {exc}")
        if self.mobile_polling:
            self.root.after(self.mobile_poll_interval, self._poll_mobile_records)

    def _apply_mobile_records(self, rows):
        """把手机记录并入当前点名表（界面、内存列表、数据库）"""
        operator = self.operator_call_sign_var.get().strip()
        station = self.station_name_var.get().strip()
        last_id = self.mobile_last_id
        applied = 0
        for r in rows:
            last_id = max(last_id, int(r.get("id") or 0))
            call_sign = (r.get("call_sign") or "").strip()
            if not call_sign or (operator and call_sign == operator):
                continue
            created = int(r.get("created_at") or time.time())
            full_time = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
            display_time = datetime.fromtimestamp(created).strftime("%H:%M")
            rank = self.current_rank
            qth = r.get("qth") or ""
            equipment = r.get("equipment") or ""
            power = r.get("power") or ""
            antenna = r.get("antenna") or ""
            access_method = r.get("access_method") or ""
            self.records.append((rank, call_sign, qth, equipment, power, access_method,
                                 antenna, display_time, full_time, operator, station))
            self.tree.insert("", "end", values=(rank, call_sign, qth, equipment,
                                               power, antenna, access_method,
                                               display_time), tags=('time_tag',))
            self.cursor.execute('''
                INSERT INTO roll_call_records
                (rank, call_sign, qth, equipment, power, antenna, access_method, confirm_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (rank, call_sign, qth, equipment, power, antenna, access_method, full_time))
            self.current_rank += 1
            applied += 1
        self.conn.commit()
        if last_id != self.mobile_last_id:
            self.mobile_last_id = last_id
            self._save_mobile_cursor()
        if applied:
            print(f"已合并 {applied} 条手机点名记录")
            self.root.update_idletasks()

    def _save_mobile_cursor(self):
        operator = self.operator_call_sign_var.get().strip()
        if not operator or not self.mobile_batch_id:
            return
        try:
            conn = self._mobile_session_conn(operator)
            conn.execute("INSERT OR REPLACE INTO mobile_session(batch_id, last_id) VALUES (?, ?)",
                         (self.mobile_batch_id, self.mobile_last_id))
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            print(f"保存手机点名游标失败: {exc}")

    def close_mobile_session(self):
        """结束点名时关闭中继批次，停止轮询"""
        self.mobile_polling = False
        batch_id = self.mobile_batch_id
        if not batch_id:
            return
        try:
            self._mobile_api("POST", "/api/mobile/batch/close",
                             payload={"batch_id": batch_id})
        except Exception as exc:
            print(f"关闭手机点名批次失败: {exc}")
        self.mobile_batch_id = ""
    
    def flush_library_sync(self, timeout=3):
        """退出前补传一次呼号库，避免最后几分钟的修改留到下次启动"""
        if not getattr(self, "master_lib_sync", None):
            return
        try:
            self.master_lib_sync.sync_now(timeout=timeout)
        except Exception as exc:
            print(f"退出前呼号库同步失败: {exc}")
    
    def on_p2p_status(self, status):
        """P2P 状态回调（后台线程），转回主线程保存并刷新指示灯"""
        try:
            self.root.after(0, lambda s=status: self._apply_p2p_status(s))
        except Exception:
            pass
    
    def _apply_p2p_status(self, status):
        """主线程中保存状态并刷新指示灯"""
        self.p2p_status = status
        self._update_p2p_status_light()
    
    def _update_p2p_status_light(self):
        """根据当前 P2P 状态更新指示灯颜色与文字"""
        canvas = getattr(self, "p2p_status_canvas", None)
        label = getattr(self, "p2p_status_label", None)
        if canvas is None or label is None:
            return
        status = getattr(self, "p2p_status", {}) or {}
        engine = getattr(self, "p2p_engine", None)
        running = bool(status.get("running"))
        peers = status.get("peers") or {}
        last_msg = status.get("last_message") or ""

        if engine is None or not status:
            color = "#888888"      # 灰：未启用
            text = "P2P: 未启用"
        elif not running:
            color = "#cc3333"      # 红：已停止
            text = "P2P: 已停止"
        elif peers:
            color = "#33aa33"      # 绿：已连接设备（最近同步过的对端）
            text = "P2P: 已连接 %d 台设备" % len(peers)
        elif status.get("relay_online"):
            color = "#2aa198"      # 青：中继已连通，但暂无其他设备
            text = "P2P: 中继已连接（未发现设备）"
        elif "异常" in last_msg or "失败" in last_msg or "错误" in last_msg:
            color = "#2b78d4"      # 蓝：未连接且最近一次同步出错
            text = "P2P: 运行中（最近一次出错）"
        else:
            color = "#e0a020"      # 黄：运行中但未发现设备
            text = "P2P: 运行中（未发现设备）"

        try:
            canvas.itemconfig("dot", fill=color)
            label.config(text=text, foreground=color)
        except Exception:
            pass
    
    def p2p_sync_now(self):
        """立即执行一次 P2P 同步"""
        if not getattr(self, "p2p_engine", None):
            self.init_p2p_sync()
        if not getattr(self, "p2p_engine", None):
            messagebox.showwarning("P2P 同步", "P2P 同步未启用，请先在“设置 → P2P 同步 → P2P 同步设置”中启用。")
            return
        self.p2p_engine.sync_now()
        messagebox.showinfo("P2P 同步", "已发起同步，稍后可在“同步状态”中查看结果。")
    
    def format_p2p_status(self, status):
        """把状态字典格式化成可读文本"""
        if not status:
            return "P2P 同步未运行。\n\n请在“设置 → P2P 同步 → P2P 同步设置”中启用。"
        lines = [
            f"运行状态: {'运行中' if status.get('running') else '已停止'}",
            f"设备名称: {status.get('device_name', '')}",
            f"设备标识: {status.get('device_id', '')}",
            f"UDP 端口: {status.get('udp_port', '')}    HTTP 端口: {status.get('http_port', '')}",
            f"中继地址: {status.get('relay_url') or '未配置（仅局域网直连）'}",
            f"中继状态: {'已连通' if status.get('relay_online') else ('配置但不可达' if status.get('relay_url') else '未配置')}",
            f"累计: 应用 {status.get('applied', 0)} / 跳过 {status.get('skipped', 0)} / 删除 {status.get('deleted', 0)}",
            f"最近同步: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status['last_sync'])) if status.get('last_sync') else '尚未同步'}",
            f"最近方式: {status.get('last_mode') or '-'}",
            f"最近结果: {status.get('last_message') or '-'}",
            "",
            "已连接设备:",
        ]
        peers = status.get("peers") or {}
        if not peers:
            lines.append("  （暂无）")
        for peer_id, info in peers.items():
            try:
                when = time.strftime('%H:%M:%S', time.localtime(info.get("time", 0)))
            except (ValueError, OSError):
                when = "-"
            lines.append(f"  {peer_id[:8]}  {info.get('mode', '')}  {when}  "
                         f"应用{info.get('applied', 0)}/跳过{info.get('skipped', 0)}")
        return "\n".join(lines)
    
    def show_p2p_status(self):
        """显示 P2P 同步状态窗口"""
        win = tk.Toplevel(self.root)
        win.title("P2P 同步状态")
        win.geometry("600x400")
        
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        
        text = tk.Text(frame, font=("Consolas", 10), wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True)
        
        def refresh():
            text.delete("1.0", tk.END)
            text.insert(tk.END, self.format_p2p_status(getattr(self, "p2p_status", {})))
        
        def sync_now():
            if getattr(self, "p2p_engine", None):
                self.p2p_engine.sync_now()
                refresh()
        
        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(button_row, text="关闭", command=win.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_row, text="立即同步", command=sync_now).pack(side=tk.RIGHT, padx=6)
        ttk.Button(button_row, text="刷新", command=refresh).pack(side=tk.RIGHT)
        
        refresh()
    
    def show_p2p_config_dialog(self):
        """P2P 同步设置对话框"""
        config = configparser.ConfigParser()
        config_path = self.get_p2p_config_path()
        if os.path.exists(config_path):
            config.read(config_path, encoding="utf-8")
        if "P2P" not in config:
            config["P2P"] = {}
        section = config["P2P"]
        
        win = tk.Toplevel(self.root)
        win.title("P2P 同步设置")
        win.geometry("560x380")
        win.resizable(False, False)
        
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        enabled_var = tk.BooleanVar(value=section.getboolean("enabled", fallback=False))
        name_var = tk.StringVar(value=section.get("device_name", "主控电脑"))
        relay_var = tk.StringVar(value=section.get("relay_url", ""))
        udp_var = tk.StringVar(value=section.get("udp_port", "4571"))
        http_var = tk.StringVar(value=section.get("http_port", "4572"))
        interval_var = tk.StringVar(value=section.get("sync_interval", "60"))
        
        ttk.Checkbutton(frame, text="启用 P2P 同步（局域网自动发现，跨网需配置轻量中继）",
                        variable=enabled_var).pack(anchor=tk.W, pady=(0, 10))
        
        for label, var in (("设备名称:", name_var), ("中继地址:", relay_var),
                           ("UDP 端口:", udp_var), ("HTTP 端口:", http_var),
                           ("同步间隔(秒):", interval_var)):
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, pady=4)
            ttk.Label(row, text=label, width=12).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        ttk.Label(frame, text="中继地址示例: http://服务器IP:4570（服务器上运行 relay_server.py）",
                  foreground="#666").pack(anchor=tk.W, pady=(6, 0))
        device_id = section.get("device_id", "") or "（首次运行自动生成）"
        ttk.Label(frame, text=f"设备标识: {device_id}", foreground="#666").pack(anchor=tk.W, pady=(4, 0))
        
        def save():
            def parse_port(var):
                try:
                    value = int(var.get().strip())
                except ValueError:
                    return None
                return value if 0 < value < 65536 else None
            
            udp_port = parse_port(udp_var)
            http_port = parse_port(http_var)
            if udp_port is None or http_port is None:
                messagebox.showwarning("提示", "端口必须是 1-65535 的数字")
                return
            if udp_port == http_port:
                messagebox.showwarning("提示", "UDP 端口与 HTTP 端口不能相同")
                return
            try:
                interval = max(15, int(interval_var.get().strip() or "60"))
            except ValueError:
                messagebox.showwarning("提示", "同步间隔必须是数字")
                return
            
            config["P2P"]["enabled"] = "True" if enabled_var.get() else "False"
            config["P2P"]["device_name"] = name_var.get().strip() or "主控电脑"
            config["P2P"]["relay_url"] = relay_var.get().strip()
            config["P2P"]["udp_port"] = str(udp_port)
            config["P2P"]["http_port"] = str(http_port)
            config["P2P"]["sync_interval"] = str(interval)
            try:
                with open(config_path, "w", encoding="utf-8") as fh:
                    config.write(fh)
            except OSError as exc:
                messagebox.showerror("错误", f"保存配置失败: {exc}")
                return
            
            self.stop_p2p_sync()
            self.init_p2p_sync()
            messagebox.showinfo("提示", "P2P 同步配置已保存")
            win.destroy()
        
        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(button_row, text="保存", command=save).pack(side=tk.RIGHT)
        ttk.Button(button_row, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=6)
        
    
    # ========== 客户端更新相关 ==========
    def get_client_version(self):
        try:
            return CLIENT_VERSION  # 使用全局版本常量
        except Exception:
            # 兜底：从窗口标题解析
            title = self.root.title()
            m = re.search(r'v(\d+\.\d+\.\d+)', title)
            return m.group(1) if m else "1.0.0"
    
    def manual_check_update(self):
        """手动检查更新 - 使用增强版更新机制"""
        if ClientUpdater is None:
            # 如果client_updater不可用，使用简化版本
            threading.Thread(target=self._check_update, daemon=True).start()
        else:
            # 使用增强版更新机制
            threading.Thread(target=self._enhanced_check_update, daemon=True).start()
    
    def _enhanced_check_update(self):
        """增强版更新检查 - 使用client_updater的更新机制"""
        try:
            # 创建更新器实例
            updater = ClientUpdater(
                current_version=CLIENT_VERSION,
                update_server_urls=[f"{API_BASE_URL}/api/client/check_update"],
                root_window=self.root
            )
            
            # 设置日志函数
            def log_message(message):
                print(f"[更新日志] {message}")
            updater.set_log_function(log_message)
            
            # 检查更新
            update_info = updater.check_for_updates()
            
            if update_info:
                # 提示用户有新版本
                if updater.prompt_update(update_info):
                    # 启动下载线程
                    threading.Thread(
                        target=updater.download_and_update,
                        args=(update_info.get('download_url', ''),),
                        daemon=True
                    ).start()
            else:
                self.root.after(0, lambda: messagebox.showinfo("检查更新", "当前已是最新版本"))
                
        except Exception as e:
            self.root.after(0, lambda e=e: messagebox.showerror("检查更新失败", str(e)))
    
    def _check_update(self):
        try:
            version = self.get_client_version()
            url = f"{API_BASE_URL}/api/client/check_update"
            resp = requests.get(url, params={"version": version}, timeout=10)
            if resp.status_code != 200:
                self.root.after(0, lambda: messagebox.showerror("检查更新失败", f"服务器返回状态码: {resp.status_code}"))
                return
            data = resp.json()
            if data.get("update_available"):
                info = data.get("update_info", {})
                latest = data.get("latest_version", "")
                desc = info.get("description", "")
                changes = "\n".join([f"- {c}" for c in info.get("changes", [])])
                package_file = info.get("package_file")
                
                def ask_download():
                    return messagebox.askyesno(
                        "发现新版本",
                        f"当前版本: {version}\n最新版本: {latest}\n\n{desc}\n\n更新内容:\n{changes}\n\n是否立即下载并安装？"
                    )
                if package_file and self.root.after(0, ask_download) is None:
                    # after调用无法直接取返回值，改为在主线程询问
                    pass
                # 使用单独方法在主线程中询问
                def prompt_and_act():
                    if messagebox.askyesno(
                        "发现新版本",
                        f"当前版本: {version}\n最新版本: {latest}\n\n{desc}\n\n更新内容:\n{changes}\n\n是否立即下载并安装？"
                    ):
                        threading.Thread(target=self._download_and_install_update, args=(package_file,), daemon=True).start()
                self.root.after(0, prompt_and_act)
            else:
                self.root.after(0, lambda: messagebox.showinfo("检查更新", data.get("message", "当前已是最新版本")))
        except Exception as e:
            self.root.after(0, lambda e=e: messagebox.showerror("检查更新失败", str(e)))
    
    def _download_and_install_update(self, package_file: str):
        try:
            if not package_file:
                self.root.after(0, lambda: messagebox.showerror("更新失败", "未获取到更新包文件名"))
                return
            import zipfile
            from pathlib import Path
            
            url = f"{API_BASE_URL}/api/client/download_update/{package_file}"
            temp_dir = Path("temp_update")
            temp_dir.mkdir(exist_ok=True)
            pkg_path = temp_dir / package_file
            
            # 下载
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    self.root.after(0, lambda: messagebox.showerror("下载失败", f"状态码: {r.status_code}"))
                    return
                with open(pkg_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            
            # 解压覆盖
            with zipfile.ZipFile(pkg_path, "r") as zf:
                zf.extractall(".")
            
            # 清理
            try:
                pkg_path.unlink(missing_ok=True)
            except Exception:
                pass
            
            self.root.after(0, lambda: messagebox.showinfo("更新完成", "更新已安装，请重启软件生效。"))
        except Exception as e:
            self.root.after(0, lambda e=e: messagebox.showerror("更新失败", str(e)))
    # ========== 客户端更新相关 结束 ==========
    
    def on_exit(self):
        """退出程序时检查是否已生成表单，并关闭数据库连接和窗口"""
        # 询问用户是否已生成表单
        result = messagebox.askyesno("确认退出", "是否已经生成表单？\n点击'是'将关闭程序，点击'否'将返回程序。")
        
        if result:  # 用户确认已生成表单
            # 关闭手机点名批次，停止手机提交
            self.close_mobile_session()
            # 创建倒计时上传窗口
            self.show_exit_countdown()
        # 如果用户点击"否"，则不执行任何操作，返回程序
            
    def show_exit_countdown(self):
        """显示退出倒计时窗口"""
        # 创建倒计时窗口
        countdown_window = tk.Toplevel(self.root)
        countdown_window.title("程序退出中")
        countdown_window.geometry("400x200")
        countdown_window.resizable(False, False)
        countdown_window.configure(bg="#f0f0f0")
        
        # 窗口居中显示
        countdown_window.update_idletasks()
        width = countdown_window.winfo_width()
        height = countdown_window.winfo_height()
        x = (countdown_window.winfo_screenwidth() // 2) - (width // 2)
        y = (countdown_window.winfo_screenheight() // 2) - (height // 2)
        countdown_window.geometry('{}x{}+{}+{}'.format(width, height, x, y))
        
        message_text = "正在关闭程序，请稍候..."
        
        # 添加提示标签
        ttk.Label(countdown_window, text=message_text, 
                  font=("SimHei", 12)).pack(pady=30)
        
        # 添加倒计时标签
        countdown_var = tk.StringVar(value="倒计时: 5 秒")
        countdown_label = ttk.Label(countdown_window, textvariable=countdown_var, 
                                  font=("SimHei", 14, "bold"))
        countdown_label.pack(pady=10)
        
        # 禁用关闭按钮
        countdown_window.protocol("WM_DELETE_WINDOW", lambda: None)
        
        # 初始化倒计时
        countdown_seconds = 5
        
        def update_countdown():
            nonlocal countdown_seconds
            
            countdown_seconds -= 1
            countdown_var.set(f"倒计时: {countdown_seconds} 秒")
            
            if countdown_seconds > 0:
                countdown_window.after(1000, update_countdown)
            else:
                # 倒计时结束，关闭程序
                # 先补传一次呼号库，再停止 P2P 同步引擎，避免其继续占用数据库
                self.flush_library_sync()
                self.stop_p2p_sync()
                try:
                    # 关闭数据库连接
                    self.conn.close()
                except sqlite3.Error:
                    pass
                
                # 关闭窗口
                countdown_window.destroy()
                self.root.destroy()
                
                # 终止程序
                self.root.quit()
        
        # 开始倒计时
        countdown_window.after(1000, update_countdown)
        

def show_agreement_dialog():
    """显示共享协议确认窗口"""
    # 检查配置文件中是否已勾选"下次不提醒"
    config = configparser.ConfigParser()
    config_file = "app_config.ini"
    skip_agreement = False
    
    if os.path.exists(config_file):
        config.read(config_file, encoding='utf-8')
        if "Settings" in config and "skip_agreement" in config["Settings"]:
            skip_agreement = config["Settings"]["skip_agreement"].lower() == "true"
    
    if skip_agreement:
        return True
    
    # 创建确认窗口
    agreement_window = tk.Toplevel()
    agreement_window.title("用户协议确认")
    agreement_window.geometry("500x300")
    agreement_window.resizable(False, False)
    agreement_window.transient()  # 设置为主窗口的子窗口
    agreement_window.grab_set()  # 模态窗口，阻止操作主窗口
    agreement_window.configure(bg="#f0f0f0")
    
    # 创建内容框架
    content_frame = ttk.Frame(agreement_window, padding=20)
    content_frame.pack(fill=tk.BOTH, expand=True)
    
    # 添加协议文本
    agreement_text = """本系统为共享点名系统，点名的输入信息将联网同步给所有使用这个软件的主控，形成共有资源其他主控可以调用，

如果您同意这样的共享协议请点击同意，否则点击取消退出。"""
    
    text_label = ttk.Label(
        content_frame, 
        text=agreement_text, 
        font=("SimHei", 11),
        wraplength=450, 
        justify=tk.LEFT
    )
    text_label.pack(pady=20)
    
    # 添加下次不提醒复选框
    skip_var = tk.BooleanVar()
    skip_checkbutton = ttk.Checkbutton(
        content_frame, 
        text="下次不提醒", 
        variable=skip_var, 
        onvalue=True, 
        offvalue=False
    )
    skip_checkbutton.pack(pady=10)
    
    # 保存用户选择的状态变量
    user_agreed = [False]  # 使用列表作为可变对象
    
    # 同意按钮回调函数
    def on_agree():
        user_agreed[0] = True
        
        # 如果勾选了下次不提醒，保存设置
        if skip_var.get():
            config["Settings"] = {"skip_agreement": "True"}
            with open(config_file, "w") as f:
                config.write(f)
        
        agreement_window.destroy()
    
    # 取消按钮回调函数
    def on_cancel():
        user_agreed[0] = False
        agreement_window.destroy()
    
    # 创建按钮框架
    button_frame = ttk.Frame(content_frame)
    button_frame.pack(side=tk.BOTTOM, pady=20)
    
    # 添加同意和取消按钮
    agree_button = ttk.Button(button_frame, text="同意", command=on_agree, width=15)
    agree_button.pack(side=tk.LEFT, padx=10)
    
    cancel_button = ttk.Button(button_frame, text="取消", command=on_cancel, width=15)
    cancel_button.pack(side=tk.LEFT, padx=10)
    
    # 窗口居中显示
    agreement_window.update_idletasks()
    width = agreement_window.winfo_width()
    height = agreement_window.winfo_height()
    x = (agreement_window.winfo_screenwidth() // 2) - (width // 2)
    y = (agreement_window.winfo_screenheight() // 2) - (height // 2)
    agreement_window.geometry('{}x{}+{}+{}'.format(width, height, x, y))
    
    # 等待窗口关闭
    agreement_window.wait_window()
    
    return user_agreed[0]


def sync_downloaded_database(master_db_path):
    """将下载的数据库中的call_sign_library表同步到本地数据库，先检查差异再同步"""
    try:
        # 检查下载的主数据库是否存在
        if not os.path.exists(master_db_path):
            print(f"下载的主数据库不存在: {master_db_path}")
            return
        
        master_conn = sqlite3.connect(master_db_path)
        master_cursor = master_conn.cursor()
        
        # 连接本地历史数据库
        local_conn = sqlite3.connect(os.path.join(APP_DIR, 'call_sign_history.db'))
        local_cursor = local_conn.cursor()
        
        # 确保本地数据库有call_sign_library表
        local_cursor.execute('''
        CREATE TABLE IF NOT EXISTS call_sign_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sign TEXT UNIQUE NOT NULL,
            qth TEXT,
            equipment TEXT,
            power TEXT,
            antenna TEXT,
            access_method TEXT
        )
        ''')
        
        # 检查是否有差异
        has_differences = False
        
        # 从主数据库读取call_sign_library表数据
        master_cursor.execute("SELECT call_sign, qth, equipment, power, antenna, access_method FROM call_sign_library")
        master_records = master_cursor.fetchall()
        
        # 从本地数据库读取call_sign_library表数据
        local_cursor.execute("SELECT call_sign, qth, equipment, power, antenna, access_method FROM call_sign_library")
        local_records = local_cursor.fetchall()
        
        # 将本地记录转换为字典以便快速查找
        local_records_dict = {record[0].strip().upper(): record for record in local_records}
        
        # 检查差异
        for master_record in master_records:
            call_sign, qth, equipment, power, antenna, access_method = master_record
            call_sign = call_sign.strip().upper()
            
            if not call_sign:
                continue
            
            if call_sign in local_records_dict:
                # 检查字段是否有差异
                local_record = local_records_dict[call_sign]
                if (local_record[1] != qth or local_record[2] != equipment or 
                    local_record[3] != power or local_record[4] != antenna or 
                    local_record[5] != access_method):
                    has_differences = True
                    break
            else:
                # 有新增记录
                has_differences = True
                break
        
        # 检查是否有本地记录在主数据库中不存在（删除的情况）
        master_call_signs = {record[0].strip().upper() for record in master_records}
        for local_record in local_records:
            if local_record[0].strip().upper() not in master_call_signs:
                has_differences = True
                break
        
        # 如果没有差异，直接返回
        if not has_differences:
            print("数据库无差异，无需同步")
            master_conn.close()
            local_conn.close()
            return
        
        # 有差异，进行同步
        print("检测到数据库差异，开始同步...")
        
        # 统计信息
        records_added = 0
        records_updated = 0
        records_deleted = 0
        
        # 先删除本地数据库中主数据库不存在的记录
        for local_record in local_records:
            call_sign = local_record[0].strip().upper()
            if call_sign not in master_call_signs:
                local_cursor.execute("DELETE FROM call_sign_library WHERE call_sign = ?", (call_sign,))
                records_deleted += 1
        
        # 同步每条记录到本地数据库
        for record in master_records:
            call_sign, qth, equipment, power, antenna, access_method = record
            call_sign = call_sign.strip().upper()
            
            if not call_sign:
                continue
            
            # 检查本地数据库中是否已存在该呼号
            local_cursor.execute("SELECT id, qth, equipment, power, antenna, access_method FROM call_sign_library WHERE call_sign = ?", (call_sign,))
            existing_record = local_cursor.fetchone()
            
            if existing_record:
                # 检查是否有实际差异
                if (existing_record[1] != qth or existing_record[2] != equipment or 
                    existing_record[3] != power or existing_record[4] != antenna or 
                    existing_record[5] != access_method):
                    # 更新已存在的记录
                    local_cursor.execute('''
                    UPDATE call_sign_library 
                    SET qth = ?, equipment = ?, power = ?, antenna = ?, access_method = ? 
                    WHERE call_sign = ?
                    ''', (qth, equipment, power, antenna, access_method, call_sign))
                    records_updated += 1
            else:
                # 插入新记录
                local_cursor.execute('''
                INSERT INTO call_sign_library (call_sign, qth, equipment, power, antenna, access_method) 
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (call_sign, qth, equipment, power, antenna, access_method))
                records_added += 1
        
        # 提交更改
        local_conn.commit()
        
        # 关闭连接
        master_conn.close()
        local_conn.close()
        
        print(f"成功同步数据库: 新增{records_added}条记录, 更新{records_updated}条记录, 删除{records_deleted}条记录")
        
        # 注意：数据库文件将被保留，不再在下次下载时销毁，以保留历史版本
    except Exception as e:
        print(f"同步数据库时发生错误: {str(e)}")

if __name__ == "__main__":
    # 先创建主窗口但隐藏
    root = tk.Tk()
    root.withdraw()  # 隐藏主窗口
    
    # 创建应用实例（但不显示）
    app = RollCallApp(root)
    
    # 显示协议确认窗口
    if not show_agreement_dialog():
        # 用户取消，退出程序
        root.destroy()
        exit()
    
    # 用户同意，显示主窗口并启动程序
    root.deiconify()  # 显示主窗口
    root.mainloop()
    # ...