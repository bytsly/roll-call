#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PI-STAR HTTP连接测试工具
功能：通过HTTP连接到PI-STAR盒子，使用用户名和密码登录，并显示登录状态
"""

import requests
import logging
import sys
import io
import argparse
import tkinter as tk
import threading
from tkinter import ttk, scrolledtext
from requests.exceptions import RequestException
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import re
import time
import os

# 设置日志配置，强制使用UTF-8编码
# 日志文件固定写到程序所在目录（打包后为 exe 目录），避免工作目录为 C:\Windows\system32 等不可写路径时启动失败
def _build_log_handlers():
    handlers = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    try:
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        handlers.append(logging.FileHandler(os.path.join(base_dir, "pi_star_http_test.log"), encoding='utf-8'))
    except Exception:
        # 目录不可写时仅使用控制台日志，不影响程序启动
        pass
    return handlers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_build_log_handlers()
)

# 最短通话时长（秒）：低于该值的记录不抓取（正在通话的最新一条除外）
# 主控可在软件“抓取参数”中修改，运行时由主程序写入本变量
MIN_CALL_DURATION = 3

# 确保标准输出使用UTF-8编码
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception as e:
        print(f"无法设置标准输出编码: {str(e)}")
else:
    # 旧版Python的兼容性处理
    import io
    try:
        # 尝试设置UTF-8编码，但只在buffer存在时执行
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except AttributeError:
        # 如果buffer不存在，跳过此设置
        pass
logger = logging.getLogger(__name__)

# 盒子系统类型常量
SYSTEM_WPSD = 'wpsd'        # WPSD (W0CHP 分支)
SYSTEM_PISTAR = 'pistar'    # 原版 Pi-Star (MW0MWZ)
SYSTEM_JSTAR = 'jstar'      # 国产 J-Star 固件
SYSTEM_UNKNOWN = 'unknown'  # 未识别

# 系统类型中文显示名
SYSTEM_DISPLAY_NAMES = {
    SYSTEM_WPSD: 'WPSD',
    SYSTEM_PISTAR: 'Pi-Star',
    SYSTEM_JSTAR: 'J-Star',
    SYSTEM_UNKNOWN: '未知(按Pi-Star兼容处理)',
}


class PiStarHttpTester:
    """PI-STAR HTTP连接测试类"""
    def __init__(self, host, username="pi-star", password="raspberry", timeout=(2, 6)):
        """
        初始化PI-STAR HTTP测试器
        
        参数:
            host: PI-STAR盒子的IP地址或主机名
            username: 登录用户名，默认为"pi-star"
            password: 登录密码，默认为"raspberry"
            timeout: (连接超时, 读取超时)，默认(2, 6)秒
        """
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url = f"http://{host}"
        self.session = requests.Session()
        self.is_logged_in = False
        self.log_callback = None  # 用于GUI模式的日志回调函数
        self._last_status_info = None  # 保存最后获取的状态信息
        self.system_type = SYSTEM_UNKNOWN  # 盒子系统类型，登录成功后自动识别
        self._main_html_cache = None  # (时间戳, 主页HTML)，登录后30秒内get_status_info复用，避免重复请求
    
    def set_log_callback(self, callback):
        """设置日志回调函数，用于在GUI中显示日志"""
        self.log_callback = callback
    
    def _log(self, level, message):
        """内部日志记录方法，同时输出到logger和回调函数，正确处理Unicode字符"""
        try:
            # 确保消息是字符串并处理Unicode字符
            if isinstance(message, str):
                # 替换非ASCII字符为安全的表示形式
                safe_message = message.encode('utf-8', errors='replace').decode('utf-8')
            else:
                safe_message = str(message)
            
            if level == 'info':
                logger.info(safe_message)
            elif level == 'error':
                logger.error(safe_message)
            elif level == 'warning':
                logger.warning(safe_message)
            
            # 如果设置了回调函数，也通过回调函数输出日志
            if self.log_callback:
                self.log_callback(f"[{level.upper()}] {safe_message}")
        except Exception as e:
            # 防止日志记录本身出错
            print(f"日志记录失败: {str(e)}")
    
    def test_connection(self):
        """测试与PI-STAR盒子的HTTP连接是否正常"""
        try:
            self._log('info', f"开始抓取连接到 {self.host}")
            
            # 测试连接 - PI-STAR可能需要认证才能访问管理页面
            response = requests.get(self.base_url, timeout=self.timeout)
            
            # 特殊处理401错误 - 表示连接成功但需要登录
            if response.status_code == 401:
                # 无页面内容，尝试从认证 realm 识别系统类型（如 Basic realm="Pi-Star"）
                detected = self.detect_system_type(response.headers.get('WWW-Authenticate', ''))
                if detected != SYSTEM_UNKNOWN:
                    self.system_type = detected
                self._log('info', f"连接成功，但需要登录: {self.host} (HTTP状态码: 401)，识别系统类型: {SYSTEM_DISPLAY_NAMES.get(self.system_type, self.system_type)}")
                return True
            
            response.raise_for_status()
            # 200 响应可直接从主页内容识别系统类型（无需登录）
            detected = self.detect_system_type(response.text)
            if detected != SYSTEM_UNKNOWN:
                self.system_type = detected
            self._log('info', f"成功连接到 {self.host}，HTTP状态码: {response.status_code}，识别系统类型: {SYSTEM_DISPLAY_NAMES.get(self.system_type, self.system_type)}")
            return True
        except RequestException as e:
            # 如果是401错误，我们已经在上面处理了
            # 这里处理其他连接错误
            self._log('error', f"连接 {self.host} 失败: {str(e)}")
            return False
    
    def login(self):
        """使用HTTP Basic认证登录PI-STAR盒子"""
        # 已登录且会话内已保存认证信息时直接复用，避免每次抓取都重新拉取主页
        if self.is_logged_in and self.session.auth:
            self._log('info', "已登录，复用现有会话")
            return True
        try:
            self._log('info', f"尝试使用用户名 '{self.username}' 登录 {self.host}")
            
            # PI-STAR使用的是HTTP Basic认证
            self._log('info', "尝试使用HTTP Basic认证")
            
            # 尝试多个可能的URL路径，增加找到正确认证页面的机会
            test_paths = [
                f"http://{self.host}",                # 主要登录地址（根路径）
                f"{self.base_url}/index.php",        # 备选路径（根目录首页）
                f"{self.base_url}/admin/index.php"   # 备选管理页面路径
            ]
            
            # 尝试每个路径进行认证
            for test_url in test_paths:
                self._log('info', f"测试认证URL: {test_url}")
                
                try:
                    # 创建会话对象并设置认证信息
                    auth_response = self.session.get(
                        test_url, 
                        auth=(self.username, self.password), 
                        timeout=self.timeout, 
                        allow_redirects=True
                    )
                    
                    # 记录详细的响应信息
                    self._log('info', f"认证响应状态码: {auth_response.status_code}")
                    self._log('info', f"认证响应URL: {auth_response.url}")
                    
                    # 检查认证是否成功
                    if auth_response.status_code == 200:
                        self.is_logged_in = True
                        self._log('info', f"登录成功: 通过HTTP Basic认证 (路径: {test_url})")
                        # 保存认证信息以便后续请求使用
                        self.session.auth = (self.username, self.password)
                        # 缓存主页HTML（仅主页路径，admin页面不缓存），供get_status_info 30秒内复用
                        if test_url in (f"http://{self.host}", f"{self.base_url}/index.php"):
                            self._main_html_cache = (time.time(), auth_response.text)
                        # 识别盒子系统类型（WPSD / Pi-Star / J-Star）；识别失败时保留连接阶段的识别结果
                        detected = self.detect_system_type(auth_response.text)
                        if detected != SYSTEM_UNKNOWN:
                            self.system_type = detected
                        self._log('info', f"识别到盒子系统类型: {SYSTEM_DISPLAY_NAMES.get(self.system_type, self.system_type)}")
                        return True
                    elif auth_response.status_code == 401:
                        self._log('info', f"需要认证但凭据无效或路径需要不同权限: {test_url}")
                    else:
                        self._log('info', f"路径 {test_url} 返回状态码: {auth_response.status_code}")
                except RequestException as e:
                    self._log('info', f"访问 {test_url} 时出错: {str(e)}")
                    
            # 所有路径都尝试失败
            self._log('warning', "登录失败: 尝试的所有路径都无法成功认证")
            return False
        except RequestException as e:
            self._log('error', f"登录过程中发生错误: {str(e)}")
            return False
    
    @staticmethod
    def detect_system_type(html_content):
        """
        根据页面内容识别盒子系统类型
        返回: 'wpsd' / 'pistar' / 'jstar' / 'unknown'
        """
        if not html_content:
            return SYSTEM_UNKNOWN
        content = html_content.lower()
        # J-Star 优先级最高：J-Star(BI7JTA) 基于 WPSD 改版，页面可能同时含 wpsd 字样
        # 高置信特征: "J-STAR Desktop" 标题 / "J-StarOS" 菜单 / 页脚 "Modified by BI7JTA"
        if 'j-star' in content or 'jstar' in content or 'bi7jta' in content:
            return SYSTEM_JSTAR
        # WPSD 特征：title/页脚含 WPSD，或引用 wpsd-css.php
        if 'wpsd' in content:
            return SYSTEM_WPSD
        # 原版 Pi-Star 特征
        if 'pi-star' in content or 'pistar' in content or 'digital voice dashboard' in content:
            return SYSTEM_PISTAR
        return SYSTEM_UNKNOWN

    def _fetch_call_records(self, main_html=None, minutes=None):
        """
        按系统类型选择不同方案抓取通话记录（单轮抓取，不限时间，最多30条）
        时间窗口过滤与呼号补足由 UI 层完成，避免盒子慢响应下的重复请求
        返回统一格式的记录列表: [{call_sign, source, duration, time, slot, ber}, ...]
        """
        records = self._fetch_call_records_once(main_html, None)
        if records:
            records = records[:30]
        return records

    def _fetch_call_records_once(self, main_html=None, minutes=None):
        """按系统类型执行一轮抓取（minutes=None 表示不限时间）"""
        # WPSD / 原版Pi-Star / J-Star：优先抓 /mmdvmhost/lh.php（HTML表格，含BER误码率）
        # J-Star(BI7JTA) 基于 WPSD 改版，lh.php 端点与 Pi-Star/WPSD 一致
        if self.system_type in (SYSTEM_WPSD, SYSTEM_PISTAR, SYSTEM_JSTAR, SYSTEM_UNKNOWN):
            records = self._fetch_lh_php(minutes)
            if records is not None:
                return records
            # WPSD / J-Star(基于WPSD) 备选：JSON API /api/?limit=30（无BER字段）
            if self.system_type in (SYSTEM_WPSD, SYSTEM_JSTAR):
                records = self._fetch_wpsd_json_api(minutes)
                if records is not None:
                    return records
            # Pi-Star 新版(4.2+)备选：JSON API /api/last_heard.php
            if self.system_type == SYSTEM_PISTAR:
                records = self._fetch_pistar_json_api(minutes)
                if records is not None:
                    return records
        # 最终回退：主页通用HTML解析
        if main_html:
            self._log('info', f"使用主页通用解析方案 (系统类型: {SYSTEM_DISPLAY_NAMES.get(self.system_type, self.system_type)})")
            return self._parse_call_records(main_html, minutes)
        # None 表示所有方案均失败（[] 表示成功抓取但没有记录）
        return None

    def _fetch_lh_php(self, minutes=None):
        """抓取 Last Heard HTML 表格端点：WPSD 4.x 为 last_heard_table.php，Pi-Star/J-Star 为 lh.php"""
        if self.system_type in (SYSTEM_WPSD, SYSTEM_JSTAR, SYSTEM_UNKNOWN):
            endpoints = ["/mmdvmhost/last_heard_table.php", "/mmdvmhost/lh.php"]
        else:
            endpoints = ["/mmdvmhost/lh.php", "/mmdvmhost/last_heard_table.php"]
        for path in endpoints:
            url = f"{self.base_url}{path}"
            try:
                self._log('info', f"尝试抓取 Last Heard 端点: {url}")
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code != 200:
                    self._log('info', f"{path} 返回状态码 {response.status_code}，尝试下一个端点")
                    continue
                if '<table' not in response.text.lower():
                    self._log('info', f"{path} 响应中无表格，尝试下一个端点")
                    continue
                records = self._parse_lh_table(response.text, minutes)
                self._log('info', f"{path} 解析完成，获得 {len(records)} 条记录")
                return records
            except RequestException as e:
                self._log('info', f"抓取 {path} 失败: {str(e)}，尝试下一个端点")
        return None

    def _fetch_wpsd_json_api(self, minutes=None):
        """抓取 WPSD 专用 JSON API: /api/?limit=30（字段无BER）"""
        url = f"{self.base_url}/api/?limit=30"
        try:
            self._log('info', f"尝试 WPSD JSON API: {url}")
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return None
            data = response.json()
            if not isinstance(data, list):
                return None
            records = []
            current_time = datetime.now()
            for index, item in enumerate(data):
                try:
                    duration = float(item.get('duration', 0) or 0)
                    # 第一条可能是正在通话的记录（时长尚未累计到3秒），也一并保留
                    if duration < MIN_CALL_DURATION and index != 0:
                        continue
                    record_time = self._parse_json_time(item.get('time_utc'), current_time)
                    if minutes is not None and record_time and record_time < current_time - timedelta(minutes=minutes):
                        continue
                    call_sign = (item.get('callsign') or '').replace('(GPS)', '').replace('(gps)', '').strip()
                    if not call_sign:
                        continue
                    records.append({
                        'call_sign': call_sign,
                        'source': (item.get('src') or '').lower(),
                        'duration': duration,
                        'time': record_time.strftime('%H:%M:%S') if record_time else '',
                        'slot': self._extract_slot_from_mode(item.get('mode') or ''),
                        'ber': '',  # WPSD JSON API 无 BER 字段
                    })
                except Exception:
                    continue
            self._log('info', f"WPSD JSON API 解析完成，获得 {len(records)} 条记录")
            return records
        except Exception as e:
            self._log('info', f"WPSD JSON API 抓取失败: {str(e)}")
            return None

    def _fetch_pistar_json_api(self, minutes=None):
        """抓取 Pi-Star 4.2+ JSON API: /api/last_heard.php"""
        url = f"{self.base_url}/api/last_heard.php?num_transmissions=30"
        try:
            self._log('info', f"尝试 Pi-Star JSON API: {url}")
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return None
            data = response.json()
            if not isinstance(data, list):
                return None
            records = []
            current_time = datetime.now()
            for index, item in enumerate(data):
                try:
                    duration = float(item.get('duration', 0) or 0)
                    # 第一条可能是正在通话的记录（时长尚未累计到3秒），也一并保留
                    if duration < MIN_CALL_DURATION and index != 0:
                        continue
                    record_time = self._parse_json_time(item.get('time_utc'), current_time)
                    if minutes is not None and record_time and record_time < current_time - timedelta(minutes=minutes):
                        continue
                    call_sign = (item.get('callsign') or '').replace('(GPS)', '').replace('(gps)', '').strip()
                    if not call_sign:
                        continue
                    ber = item.get('bit_error_rate') or ''
                    records.append({
                        'call_sign': call_sign,
                        'source': (item.get('source') or item.get('src') or '').lower(),
                        'duration': duration,
                        'time': record_time.strftime('%H:%M:%S') if record_time else '',
                        'slot': self._extract_slot_from_mode(item.get('mode') or ''),
                        'ber': str(ber),
                    })
                except Exception:
                    continue
            self._log('info', f"Pi-Star JSON API 解析完成，获得 {len(records)} 条记录")
            return records
        except Exception as e:
            self._log('info', f"Pi-Star JSON API 抓取失败: {str(e)}")
            return None

    @staticmethod
    def parse_time_text(time_text, current_time):
        """解析 Last Heard 时间列文本，支持 '12:34:56 Oct 25th'、'09:43:39 PM Nov 9'、'21:43:39' 等格式"""
        try:
            text = (time_text or '').replace('\u202f', ' ').strip()
            if not text:
                return current_time
            match = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?', text, re.IGNORECASE)
            if not match:
                return current_time
            hour = int(match.group(1))
            minute = int(match.group(2))
            second = int(match.group(3) or 0)
            meridiem = (match.group(4) or '').upper()
            if meridiem == 'PM' and hour < 12:
                hour += 12
            elif meridiem == 'AM' and hour == 12:
                hour = 0
            month_map = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                         'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
            month = current_time.month
            day = current_time.day
            month_match = re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', text, re.IGNORECASE)
            if month_match:
                month = month_map[month_match.group(1)[:3].lower()]
                day_match = re.search(r'(\d{1,2})', text[month_match.end():])
                if day_match:
                    day = int(day_match.group(1))
            parsed = datetime(current_time.year, month, day, hour, minute, second)
            return PiStarHttpTester.fix_tz_offset(parsed, current_time)
        except Exception:
            return current_time

    @staticmethod
    def fix_tz_offset(parsed, current_time):
        """启发式时区修正：盒子 Last Heard 页面可能输出 UTC 或本地时间（取决于盒子时区配置）。
        仅当"按本地解释的差距"≈本机UTC偏移（±1.5小时窗口）且修正后明显更接近当前时间时，
        判定为 UTC 输出并修正为本地时间；否则保持原值（本地时区盒子不受影响）。"""
        try:
            utc_offset = (datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        except Exception:
            return parsed
        if abs(utc_offset) < 3600:  # 本机时区接近UTC，无需修正
            return parsed
        raw_delta = (current_time - parsed).total_seconds()
        # 差距需≈一个时区偏移（±1.5小时窗口），否则视为本地时间不动
        if abs(abs(raw_delta) - abs(utc_offset)) > 5400:
            return parsed
        corrected = parsed + timedelta(seconds=utc_offset)
        fixed_delta = (current_time - corrected).total_seconds()
        # 修正后落未来超过10分钟：跨天场景，回退一天
        if fixed_delta < -600:
            corrected -= timedelta(days=1)
            fixed_delta = (current_time - corrected).total_seconds()
        # 修正后明显更接近现在（至少改善30分钟）才采用
        if abs(fixed_delta) < abs(raw_delta) - 1800:
            return corrected
        return parsed

    @staticmethod
    def _parse_json_time(time_val, current_time):
        """解析 JSON API 的时间字段（可能是时间戳或字符串），返回本地 datetime"""
        if time_val is None or time_val == '':
            return current_time
        # Unix 时间戳（秒/毫秒）
        try:
            ts = float(time_val)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        except (ValueError, TypeError, OSError):
            pass
        # 字符串格式 "YYYY-MM-DD HH:MM:SS"（按UTC处理转本地）
        if isinstance(time_val, str):
            for fmt in ('%Y-%m-%d %H:%M:%S', '%H:%M:%S'):
                try:
                    parsed = datetime.strptime(time_val.strip(), fmt)
                    if fmt == '%H:%M:%S':
                        parsed = parsed.replace(year=current_time.year, month=current_time.month, day=current_time.day)
                        # HH:MM:SS 无时区信息，按启发式修正（UTC输出的盒子自动转本地）
                        return PiStarHttpTester.fix_tz_offset(parsed, current_time)
                    # UTC 字符串转本地时间
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    return parsed + (datetime.now() - utc_now)
                except ValueError:
                    continue
        return current_time

    @staticmethod
    def _extract_slot_from_mode(mode_text):
        """从模式文本中提取时隙，如 'DMR TS 1' / 'DMR TS2' -> '时隙1'"""
        ts_match = re.search(r'ts\s*(\d+)', mode_text.lower())
        if ts_match:
            return f"时隙{ts_match.group(1)}"
        return ''

    def _parse_lh_table(self, html_content, minutes=None):
        """
        解析 Last Heard HTML 表格（WPSD: /mmdvmhost/last_heard_table.php，Pi-Star: /mmdvmhost/lh.php）
        表头典型格式: Time | Mode | Callsign | Target | Source | Dur(s) | Loss | BER（WPSD 可能为中文表头）
        返回与 _parse_call_records 相同结构的记录列表（时长>=MIN_CALL_DURATION 秒，minutes分钟内；minutes=None 不限时间）
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        call_records = []
        current_time = datetime.now()
        time_cutoff = current_time - timedelta(minutes=minutes) if minutes is not None else None

        table = soup.find('table')
        if not table:
            return call_records
        rows = table.find_all('tr')
        if not rows:
            return call_records

        # 定位各列索引（兼容英文表头与 WPSD 中文表头：时间/呼号/模式/源/时长）
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(['th', 'td'])]
        idx = {'time': None, 'mode': None, 'call': None, 'source': None, 'dur': None, 'ber': None}
        for i, h in enumerate(headers):
            if 'time' in h or '时间' in h:
                idx['time'] = i
            elif 'mode' in h or '模式' in h:
                idx['mode'] = i
            elif 'call' in h or '呼号' in h:
                idx['call'] = i
            elif 'source' in h or 'src' in h or '源' in h:
                idx['source'] = i
            elif 'dur' in h or 'sec' in h or '时长' in h:
                idx['dur'] = i
            elif 'ber' in h or '误码' in h:
                idx['ber'] = i
        if idx['call'] is None or idx['dur'] is None:
            self._log('info', f"lh.php 表头无法识别: {headers}")
            return call_records

        for row_idx, row in enumerate(rows[1:]):
            cells = row.find_all(['td', 'th'])
            try:
                if len(cells) <= max(idx['call'], idx['dur']):
                    continue
                # 时长过滤 ≥ MIN_CALL_DURATION 秒（WPSD 为小数秒，如 1.6）；表格第一行可能是正在通话的记录（时长未写入或不足阈值），也一并抓取
                dur_text = cells[idx['dur']].get_text(strip=True)
                dur_match = re.search(r'(\d+(?:\.\d+)?)', dur_text)
                duration = float(dur_match.group(1)) if dur_match else 0.0
                if duration < MIN_CALL_DURATION and row_idx != 0:
                    continue
                # 呼号（去掉GPS标识）
                call_sign = cells[idx['call']].get_text(strip=True)
                call_sign = call_sign.replace('(GPS)', '').replace('(gps)', '').strip()
                if not call_sign:
                    continue
                # 来源
                source = cells[idx['source']].get_text(strip=True).lower() if idx['source'] is not None and len(cells) > idx['source'] else ''
                # 时间（如 "12:34:56 Oct 25th"、"09:43:39 PM Nov 9"），并做时间窗口过滤
                time_text = cells[idx['time']].get_text(strip=True) if idx['time'] is not None and len(cells) > idx['time'] else ''
                record_time = self.parse_time_text(time_text, current_time)
                if time_cutoff is not None and record_time < time_cutoff:
                    continue
                # 时隙：从 Mode 列提取 "DMR TS 1"
                slot = ''
                if idx['mode'] is not None and len(cells) > idx['mode']:
                    slot = self._extract_slot_from_mode(cells[idx['mode']].get_text(strip=True))
                # 误码率
                ber = ''
                if idx['ber'] is not None and len(cells) > idx['ber']:
                    ber_match = re.search(r'(\d+(?:\.\d+)?\s*%)', cells[idx['ber']].get_text(strip=True))
                    if ber_match:
                        ber = ber_match.group(1)
                call_records.append({
                    'call_sign': call_sign,
                    'source': source,
                    'duration': duration,
                    'time': time_text,
                    'slot': slot,
                    'ber': ber,
                })
            except Exception:
                continue
        return call_records

    def get_status_info(self):
        """获取PI-STAR盒子的状态信息，包括通话记录和transmitted mode（需要先登录）"""
        if not self.is_logged_in:
            self._log('warning', "请先登录后再获取状态信息")
            return None
        
        try:
            self._log('info', "尝试获取PI-STAR盒子状态信息")
            
            # 初始化transmitted mode
            self.transmitted_mode = None
            
            # 登录后30秒内复用主页缓存，避免重复请求（盒子响应慢，每次约3秒）
            main_html = None
            if self._main_html_cache and (time.time() - self._main_html_cache[0]) < 30:
                main_html = self._main_html_cache[1]
                self._log('info', f"复用缓存主页（{len(main_html)}字节），跳过主页请求")
            
            # 优先抓取轻量端点（Last Heard 表格 / JSON API），避免每次“抓取呼号”都拉一次主页
            # 时间窗口过滤由界面层完成（minutes=None 多取一些，便于不足5条时用最近记录补足）
            call_records = self._fetch_call_records_once(None, None)
            if call_records is None:
                self._log('info', "轻量端点不可用，回退主页解析")
                if main_html is None:
                    main_html = self._fetch_main_html()
                if main_html is None:
                    self._log('error', "获取状态信息失败: 尝试的所有路径都无法访问状态信息页面")
                    return None
                call_records = self._parse_call_records(main_html, None)
            
            # 解析transmitted mode（只从已缓存/已抓取的主页中提取，不额外请求）
            if main_html:
                self.transmitted_mode = self._parse_transmitted_mode(main_html)
                if self.transmitted_mode:
                    self._log('info', f"成功提取transmitted mode: {self.transmitted_mode}")
                else:
                    self._log('info', "未找到transmitted mode字段")
            
            call_records = (call_records or [])[:30]
            if call_records:
                self._log('info', f"成功提取到 {len(call_records)} 条符合条件的通话记录")
                for record in call_records:
                    # 格式化通话记录信息，确保特殊字符被正确处理并输出所有字段
                    call_sign = record['call_sign'].encode('utf-8', errors='replace').decode('utf-8')
                    record_time_str = record['time'].encode('utf-8', errors='replace').decode('utf-8')
                    source = record['source'].encode('utf-8', errors='replace').decode('utf-8')
                    duration = record['duration']
                    # 提取时隙信息
                    slot = record.get('slot', '无')  # 默认显示'无'，确保总是有值
                    # 确保slot不为空字符串或None
                    if not slot or slot.strip() == '':
                        slot = '无'
                    slot_info = f", 时隙={slot}"
                    self._log('info', f"通话记录: 呼号={call_sign}, 时间={record_time_str}, 时长={duration}秒, 来源={source}{slot_info}")
            else:
                self._log('info', "未找到符合条件的通话记录")
            
            # 保存最后获取的状态信息
            self._last_status_info = call_records
            # 返回包含call_records键的字典，以便主程序正确处理
            return {'call_records': call_records}
        except Exception as e:
            self._log('error', f"获取状态信息过程中发生未知错误: {str(e)}")
            return None

    def _fetch_main_html(self):
        """抓取盒子主页HTML（用于transmitted mode与记录回退解析），成功时更新缓存"""
        status_paths = [
            f"http://{self.host}",                # 主要状态信息页面（根路径）
            f"{self.base_url}/index.php",        # 备选路径（根目录首页）
            f"{self.base_url}/admin/index.php"   # 备选管理页面路径
        ]
        for status_url in status_paths:
            self._log('info', f"尝试获取状态信息URL: {status_url}")
            try:
                response = self.session.get(status_url, timeout=self.timeout)
                response.raise_for_status()
                self._log('info', f"成功获取状态信息 (路径: {status_url})，响应长度: {len(response.text)} 字节")
                if status_url != status_paths[2]:
                    self._main_html_cache = (time.time(), response.text)
                return response.text
            except RequestException as e:
                self._log('info', f"获取状态信息失败 (路径: {status_url}): {str(e)}")
        return None
            
    def _parse_transmitted_mode(self, html_content):
        """解析HTML内容，提取transmitted mode字段"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 策略1: 查找包含'transmitted mode'的标签
            mode_elements = soup.find_all(lambda tag: 'transmitted' in tag.get_text().lower() and 'mode' in tag.get_text().lower())
            for element in mode_elements:
                # 查找后面的兄弟元素
                next_element = element.find_next_sibling()
                if next_element:
                    mode_text = next_element.get_text(strip=True)
                    if mode_text:
                        return mode_text
                
                # 查找父元素内的所有文本
                parent_text = element.parent.get_text(strip=True).lower()
                if ':' in parent_text:
                    mode_part = parent_text.split(':')[1].strip()
                    if mode_part:
                        return mode_part
            
            # 策略2: 查找包含'tx mode'的标签
            tx_mode_elements = soup.find_all(lambda tag: 'tx' in tag.get_text().lower() and 'mode' in tag.get_text().lower())
            for element in tx_mode_elements:
                # 查找后面的兄弟元素
                next_element = element.find_next_sibling()
                if next_element:
                    mode_text = next_element.get_text(strip=True)
                    if mode_text:
                        return mode_text
                
                # 查找父元素内的所有文本
                parent_text = element.parent.get_text(strip=True).lower()
                if ':' in parent_text:
                    mode_part = parent_text.split(':')[1].strip()
                    if mode_part:
                        return mode_part
            
            # 策略3: 使用正则表达式直接搜索
            mode_match = re.search(r'transmitted\s*mode[:\s]+([a-z0-9-]+)', html_content.lower())
            if mode_match:
                return mode_match.group(1).upper()
                
            # 策略4: 搜索TX Mode格式
            tx_mode_match = re.search(r'tx\s*mode[:\s]+([a-z0-9-]+)', html_content.lower())
            if tx_mode_match:
                return tx_mode_match.group(1).upper()
                
            return None
        except Exception as e:
            self._log('error', f"解析transmitted mode时出错: {str(e)}")
            return None
            
    def _parse_call_records(self, html_content, minutes=None):
        """解析HTML内容，提取符合条件的通话记录（源为net，时长>=MIN_CALL_DURATION，minutes分钟内的记录；minutes=None 不限时间）"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            call_records = []
            
            # 获取当前时间，用于过滤minutes分钟内的记录
            current_time = datetime.now()
            three_minutes_ago = current_time - timedelta(minutes=minutes) if minutes is not None else None
            
            self._log('info', "开始解析通话记录")
            
            # 策略1: 查找可能包含通话记录的表格，精确识别表头字段
            self._log('info', "策略1: 尝试查找通话记录表格")
            tables = soup.find_all('table')
            for table in tables:
                # 检查表格是否可能包含通话记录
                table_text = table.get_text().lower()
                if 'sec' in table_text or 'seconds' in table_text or 'duration' in table_text:
                    self._log('info', f"找到可能包含通话记录的表格")
                    rows = table.find_all('tr')
                    
                    if rows:
                        # 获取表头
                        header_cells = rows[0].find_all(['th', 'td'])
                        headers = [th.get_text(strip=True).lower() for th in header_cells]
                        
                        # 记录表头信息以便调试
                        self._log('info', f"表头: {headers}")
                        
                        # 精确查找各个字段的索引
                        call_sign_index = None
                        source_index = None
                        duration_index = None
                        time_index = None
                        slot_index = None
                        
                        for i, header in enumerate(headers):
                            # 精确查找呼号字段（callsign）
                            if 'callsign' in header or '呼号' in header:
                                call_sign_index = i
                            # 精确查找源字段（source）
                            if 'source' in header or '源' in header:
                                source_index = i
                            # 精确查找时长字段（duration或sec）
                            if 'duration' in header or 'sec' in header or '时长' in header:
                                duration_index = i
                            # 查找时间字段
                            if 'time' in header or '时间' in header:
                                time_index = i
                            # 查找时隙字段
                            if 'slot' in header or 'timeslot' in header or '时隙' in header:
                                slot_index = i
                            # 查找模式字段
                            if 'mode' in header or '模式' in header:
                                mode_index = i
                            # 查找误码率字段
                            if 'ber' in header or '误码率' in header:
                                ber_index = i
                        
                        # 如果找到了所有必要的字段，使用这些精确的索引
                        if call_sign_index is not None and source_index is not None and duration_index is not None:
                            self._log('info', f"找到了精确的字段索引 - 呼号列: {call_sign_index}, 源列: {source_index}, 时长列: {duration_index}")
                            
                            # 遍历数据行
                            for row in rows[1:]:  # 跳过表头
                                cells = row.find_all(['td', 'th'])
                                if len(cells) > max(call_sign_index, source_index, duration_index):
                                    try:
                                        # 从精确的列索引提取数据
                                        source = cells[source_index].get_text(strip=True).lower()
                                         
                                        # 不再限制来源，提取所有通话记录
                                        # if 'net' in source:
                                        # 提取时长
                                        duration_text = cells[duration_index].get_text(strip=True)
                                        duration_match = re.search(r'(\d+)', duration_text)
                                        if duration_match:
                                            duration = int(duration_match.group(1))
                                            # 筛选时长3秒以上的记录
                                            if duration >= MIN_CALL_DURATION:
                                                # 提取呼号并隐藏GPS标识
                                                call_sign = cells[call_sign_index].get_text(strip=True)
                                                # 隐藏GPS标识
                                                if '(GPS)' in call_sign:
                                                    call_sign = call_sign.replace('(GPS)', '')
                                                elif '(gps)' in call_sign:
                                                    call_sign = call_sign.replace('(gps)', '')
                                                
                                                # 提取时隙信息
                                                slot = ''
                                                # 首先尝试从时隙列提取
                                                if slot_index is not None and len(cells) > slot_index:
                                                    slot_text = cells[slot_index].get_text(strip=True)
                                                    # 从文本中提取时隙数字
                                                    slot_match = re.search(r'(\d+)', slot_text)
                                                    if slot_match:
                                                        slot = f"时隙{slot_match.group(1)}"
                                                
                                                # 如果从时隙列未提取到信息，尝试从模式列提取
                                                if not slot and 'mode_index' in locals() and mode_index is not None and len(cells) > mode_index:
                                                    mode_text = cells[mode_index].get_text(strip=True)
                                                    # 从模式文本中提取时隙信息（DMR TS1 或 DMR TS2）
                                                    ts_match = re.search(r'(?:dmr\s+)?ts(\d+)', mode_text.lower())
                                                    if ts_match:
                                                        slot = f"时隙{ts_match.group(1)}"
                                                
                                                # 提取误码率信息
                                                ber = ''
                                                if 'ber_index' in locals() and ber_index is not None and len(cells) > ber_index:
                                                    ber_text = cells[ber_index].get_text(strip=True)
                                                    # 从文本中提取误码率值
                                                    ber_match = re.search(r'(\d+(?:\.\d+)?\s*%)', ber_text)
                                                    if ber_match:
                                                        ber = ber_match.group(1)
                                                
                                                # 尝试提取时间信息
                                                time = ''
                                                record_time = current_time  # 默认使用当前时间
                                                if time_index is not None and len(cells) > time_index:
                                                    time = cells[time_index].get_text(strip=True)
                                                       
                                                    # 尝试解析时间
                                                    try:
                                                        # 假设时间格式为 HH:MM:SS 或 MM:SS
                                                        if len(time.split(':')) == 3:
                                                            # 带小时的格式
                                                            time_parts = time.split(':')
                                                            record_time = datetime(current_time.year, current_time.month, current_time.day, 
                                                                                 int(time_parts[0]), int(time_parts[1]), int(time_parts[2]))
                                                        elif len(time.split(':')) == 2:
                                                            # 只有分钟和秒的格式
                                                            time_parts = time.split(':')
                                                            record_time = datetime(current_time.year, current_time.month, current_time.day, 
                                                                                 0, int(time_parts[0]), int(time_parts[1]))
                                                    except:
                                                        # 解析失败，保持使用当前时间
                                                        pass
                                                
                                                # 检查是否在3分钟内
                                                if three_minutes_ago is None or record_time >= three_minutes_ago:
                                                    call_records.append({
                                                        'call_sign': call_sign,
                                                        'source': source,
                                                        'duration': duration,
                                                        'time': time,
                                                        'slot': slot,
                                                        'ber': ber
                                                    })
                                    except Exception as e:
                                        self._log('error', f"解析行时出错: {str(e)}")
                                        continue
                        else:
                            # 如果没有找到精确的字段索引，回退到之前的通用方法
                            self._log('info', "未找到精确的字段索引，使用通用解析方法")
                            for row in rows[1:]:  # 跳过表头
                                cells = row.find_all(['td', 'th'])
                                if len(cells) >= 3:
                                    # 尝试不同的列索引组合
                                    for i in range(len(cells)-2):
                                        try:
                                            # 提取可能的呼号、来源和时长信息
                                            call_sign = cells[i].get_text(strip=True)
                                            # 隐藏GPS标识
                                            if '(GPS)' in call_sign:
                                                call_sign = call_sign.replace('(GPS)', '')
                                            elif '(gps)' in call_sign:
                                                call_sign = call_sign.replace('(gps)', '')
                                            source = cells[i+1].get_text(strip=True).lower() if i+1 < len(cells) else ''
                                            duration_text = cells[i+2].get_text(strip=True) if i+2 < len(cells) else ''
                                              
                                            # 不再限制来源，提取所有通话记录
                                            # if 'net' in source:
                                            # 提取时长（秒）
                                            duration_match = re.search(r'(\d+)', duration_text)
                                            if duration_match:
                                                duration = int(duration_match.group(1))
                                                # 筛选时长3秒以上的记录
                                                if duration >= MIN_CALL_DURATION:
                                                    # 尝试获取时间信息
                                                    time = ''
                                                    if i+3 < len(cells):
                                                        time = cells[i+3].get_text(strip=True)
                                                    
                                                    # 初始化slot变量为空字符串
                                                    slot = ''
                                                    # 尝试从其他列提取模式信息以获取时隙
                                                    for cell in cells:
                                                        cell_text = cell.get_text(strip=True).lower()
                                                        ts_match = re.search(r'(?:dmr\s+)?ts(\d+)', cell_text)
                                                        if ts_match:
                                                            slot = f"时隙{ts_match.group(1)}"
                                                            break
                                                       
                                                    call_records.append({
                                                        'call_sign': call_sign,
                                                        'source': source,
                                                        'duration': duration,
                                                        'time': time,
                                                        'slot': slot,
                                                        'ber': ''
                                                    })
                                        except Exception:
                                            continue
            
            # 策略2: 查找最近活动/Recent Activity区域
            if not call_records:
                self._log('info', "策略2: 尝试查找Recent Activity区域")
                recent_activity = soup.find(lambda tag: tag.name in ['div', 'section'] and 'recent' in tag.get_text().lower())
                if recent_activity:
                    self._log('info', "找到Recent Activity区域")
                    # 查找可能的通话记录条目
                    entries = recent_activity.find_all(['div', 'p', 'li'])
                    for entry in entries:
                        text = entry.get_text().lower()
                        if 'sec' in text or 'seconds' in text or '秒' in text:                            # 提取呼号
                            call_sign_match = re.search(r'[a-z0-9]{1,3}[0-9][a-z0-9]{0,3}', text, re.IGNORECASE)
                            if call_sign_match:
                                call_sign = call_sign_match.group(0)
                                # 隐藏GPS标识
                                if '(GPS)' in call_sign:
                                    call_sign = call_sign.replace('(GPS)', '')
                                elif '(gps)' in call_sign:
                                    call_sign = call_sign.replace('(gps)', '')
                                # 隐藏GPS标识
                                if '(GPS)' in call_sign:
                                    call_sign = call_sign.replace('(GPS)', '')
                                elif '(gps)' in call_sign:
                                    call_sign = call_sign.replace('(gps)', '')
                                # 提取时长
                                duration_match = re.search(r'(\d+)\s*(?:sec|seconds|秒)', text)
                                if duration_match:
                                    duration = int(duration_match.group(1))
                                    # 筛选时长3秒以上的记录，与策略1保持一致
                                    if duration >= MIN_CALL_DURATION:
                                        # 提取时间
                                        time_match = re.search(r'(\d{1,2}:\d{2}:\d{2})|(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})', text)
                                        time = time_match.group(0) if time_match else ''
                                        
                                        # 提取时隙信息
                                        slot = ''
                                        # 尝试匹配多种格式：slot/时隙 X、DMR TSX
                                        slot_match = re.search(r'(?:slot|时隙)\s*(\d+)|(?:dmr\s+)?ts(\d+)', text, re.IGNORECASE)
                                        if slot_match:
                                            # 获取第一个非None的捕获组
                                            slot_num = slot_match.group(1) if slot_match.group(1) else slot_match.group(2)
                                            slot = f"时隙{slot_num}"
                                        
                                        # 提取误码率信息
                                        ber = ''
                                        ber_match = re.search(r'(ber|误码率)\s*[:=]?\s*(\d+(?:\.\d+)?\s*%)', text, re.IGNORECASE)
                                        if ber_match:
                                            ber = ber_match.group(2)
                                        
                                        # 尝试解析时间
                                        record_time = current_time  # 默认使用当前时间
                                        try:
                                            if time_match:
                                                time_str = time_match.group(0)
                                                # 检查是否包含日期
                                                if '-' in time_str:
                                                    # 格式: MM-DD HH:MM 或类似
                                                    parts = time_str.split(' ')
                                                    if len(parts) == 2:
                                                        date_part, time_part = parts
                                                        month, day = map(int, date_part.split('-'))
                                                        hour, minute = map(int, time_part.split(':'))
                                                        record_time = datetime(current_time.year, month, day, hour, minute, 0)
                                                else:
                                                    # 格式: HH:MM:SS 或 HH:MM
                                                    time_parts = time_str.split(':')
                                                    if len(time_parts) == 3:
                                                        hour, minute, second = map(int, time_parts)
                                                        record_time = self.fix_tz_offset(datetime(current_time.year, current_time.month, current_time.day, hour, minute, second), current_time)
                                                    elif len(time_parts) == 2:
                                                        hour, minute = map(int, time_parts)
                                                        record_time = datetime(current_time.year, current_time.month, current_time.day, hour, minute, 0)
                                        except:
                                            # 解析失败，保持使用当前时间
                                            pass
                                        
                                        # 检查是否在3分钟内
                                        if three_minutes_ago is None or record_time >= three_minutes_ago:
                                            call_records.append({
                                                'call_sign': call_sign,
                                                'duration': duration,
                                                'time': time,
                                                'slot': slot,
                                                'ber': ber
                                            })
            
            # 策略3: 查找包含"NET"标识的所有元素
            if not call_records:
                self._log('info', "策略3: 尝试查找所有可能包含通话记录的元素")
                net_elements = soup.find_all(lambda tag: tag.name != 'script' and tag.name != 'style')
                for element in net_elements:
                    text = element.get_text().lower()
                    # 检查是否包含通话时长信息
                    if 'sec' in text or 'seconds' in text or '秒' in text:
                        # 提取呼号
                        call_sign_match = re.search(r'[a-z0-9]{1,3}[0-9][a-z0-9]{0,3}', text, re.IGNORECASE)
                        if call_sign_match:
                            call_sign = call_sign_match.group(0)
                            # 隐藏GPS标识
                            if '(GPS)' in call_sign:
                                call_sign = call_sign.replace('(GPS)', '')
                            elif '(gps)' in call_sign:
                                call_sign = call_sign.replace('(gps)', '')
                            # 提取时长
                            duration_match = re.search(r'(\d+)\s*(?:sec|seconds|秒)', text)
                            if duration_match:
                                duration = int(duration_match.group(1))
                                # 筛选时长3秒以上的记录，与策略1保持一致
                                if duration >= MIN_CALL_DURATION:
                                    # 提取时间
                                    time_match = re.search(r'(\d{1,2}:\d{2}:\d{2})|(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})', text)
                                    time = time_match.group(0) if time_match else ''
                                    
                                    # 提取时隙信息
                                    slot = ''
                                    # 尝试匹配多种格式：slot/时隙 X、DMR TSX
                                    slot_match = re.search(r'(?:slot|时隙)\s*(\d+)|(?:dmr\s+)?ts(\d+)', text, re.IGNORECASE)
                                    if slot_match:
                                        # 获取第一个非None的捕获组
                                        slot_num = slot_match.group(1) if slot_match.group(1) else slot_match.group(2)
                                        slot = f"时隙{slot_num}"
                                    
                                    # 提取误码率信息
                                    ber = ''
                                    ber_match = re.search(r'(ber|误码率)\s*[:=]?\s*(\d+(?:\.\d+)?\s*%)', text, re.IGNORECASE)
                                    if ber_match:
                                        ber = ber_match.group(2)
                                    
                                    # 尝试解析时间
                                    record_time = current_time  # 默认使用当前时间
                                    try:
                                        if time_match:
                                            time_str = time_match.group(0)
                                            # 检查是否包含日期
                                            if '-' in time_str:
                                                # 格式: MM-DD HH:MM 或类似
                                                parts = time_str.split(' ')
                                                if len(parts) == 2:
                                                    date_part, time_part = parts
                                                    month, day = map(int, date_part.split('-'))
                                                    hour, minute = map(int, time_part.split(':'))
                                                    record_time = datetime(current_time.year, month, day, hour, minute, 0)
                                            else:
                                                # 格式: HH:MM:SS 或 HH:MM
                                                time_parts = time_str.split(':')
                                                if len(time_parts) == 3:
                                                    hour, minute, second = map(int, time_parts)
                                                    record_time = self.fix_tz_offset(datetime(current_time.year, current_time.month, current_time.day, hour, minute, second), current_time)
                                                elif len(time_parts) == 2:
                                                    hour, minute = map(int, time_parts)
                                                    record_time = datetime(current_time.year, current_time.month, current_time.day, hour, minute, 0)
                                    except:
                                        # 解析失败，保持使用当前时间
                                        pass
                                    
                                    # 检查是否在3分钟内
                                    if three_minutes_ago is None or record_time >= three_minutes_ago:
                                        call_records.append({
                                            'call_sign': call_sign,
                                            'duration': duration,
                                            'time': time,
                                            'slot': slot,
                                            'ber': ber
                                        })
            
            # 输出解析结果统计信息
            if call_records:
                self._log('info', f"解析完成，找到 {len(call_records)} 条符合条件的通话记录")
            else:
                self._log('info', "解析完成，未找到符合条件的通话记录")
                # 保存页面内容片段用于调试
                sample_content = html_content[:500]  # 只保存前500个字符
                self._log('info', f"页面内容样本: {sample_content}...")
            
            return call_records
        except Exception as e:
            self._log('error', f"解析通话记录时出错: {str(e)}")
            return []
    
    def run_full_test(self):
        """运行完整的测试流程：连接、登录、获取状态"""
        self._log('info', f"===== PI-STAR HTTP测试开始 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) =====")
        
        # 测试连接
        if not self.test_connection():
            self._log('error', "测试失败：无法连接到PI-STAR盒子")
            return False
        
        # 测试登录
        if not self.login():
            self._log('error', "测试失败：登录PI-STAR盒子失败")
            return False
        
        # 获取状态信息
        status_info = self.get_status_info()
        if status_info:
            self._log('info', "测试成功：已成功连接并登录PI-STAR盒子")
        
        self._log('info', "===== PI-STAR HTTP测试结束 =====")
        return True

class PiStarHttpTestApp:
    """PI-STAR HTTP测试工具图形界面类"""
    def __init__(self, root):
        self.root = root
        self.root.title("PI-STAR HTTP测试工具")
        self.root.geometry("700x500")
        self.root.minsize(600, 400)
        
        # 设置中文字体支持
        self.style = ttk.Style()
        self.style.configure("TLabel", font=("SimHei", 10))
        self.style.configure("TButton", font=("SimHei", 10))
        self.style.configure("TEntry", font=("SimHei", 10))
        
        # 创建主框架
        self.main_frame = ttk.Frame(root, padding="10")
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建输入区域
        self.create_input_frame()
        
        # 创建日志显示区域
        self.create_log_frame()
        
        # 创建按钮区域
        self.create_button_frame()
        
        # 测试状态变量
        self.is_continuous_testing = False
        self.continuous_test_thread = None
        self.continuous_test_stop_event = threading.Event()
        
    def create_input_frame(self):
        """创建输入区域"""
        input_frame = ttk.LabelFrame(self.main_frame, text="连接信息", padding="10")
        input_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 配置grid列，让备选框更靠左
        input_frame.grid_columnconfigure(0, weight=0)  # 第0列（标签列）不分配额外空间
        for i in range(1, 7):  # 为备选框列（1-6列）设置相同权重
            input_frame.grid_columnconfigure(i, weight=1, uniform="same_group")
        
        # IP地址输入
        ttk.Label(input_frame, text="盒子地址:").grid(row=0, column=0, sticky=tk.W, pady=1)
        self.host_entry = ttk.Entry(input_frame, width=35)  # 设置固定宽度为30
        self.host_entry.grid(row=0, column=1, sticky=tk.W, pady=1)  # 设置为仅西侧粘性
        self.host_entry.insert(0, "192.168.0.1")  # 默认值，匹配实际设备地址
        
        # 时隙选择单选框
        self.timeslot_var = tk.IntVar(value=1)  # 默认选择时隙1
        ttk.Radiobutton(input_frame, text="时隙1", variable=self.timeslot_var, value=1).grid(row=0, column=2, padx=(10, 5), pady=1, sticky=tk.W)
        ttk.Radiobutton(input_frame, text="时隙2", variable=self.timeslot_var, value=2).grid(row=0, column=3, padx=(5, 10), pady=1, sticky=tk.W)
        
        # 连接按钮
        self.connect_button = ttk.Button(
            input_frame, 
            text="连接",
            command=self.connect_test
        )
        self.connect_button.grid(row=0, column=4, padx=(10, 10), pady=5)
        
        # 抓取数据按钮
        self.test_button = ttk.Button(
            input_frame,
            text="抓取数据",
            command=self.run_test
        )
        self.test_button.grid(row=0, column=5, padx=(10, 10), pady=5)
        
        # 连接状态显示
        self.connection_status_var = tk.StringVar(value="未连接")
        self.connection_status_label = ttk.Label(
            input_frame,
            textvariable=self.connection_status_var,
            foreground="red",
            width=15
        )
        self.connection_status_label.grid(row=0, column=6, sticky=tk.W, pady=5)
        
        # Transmitted Mode显示
        ttk.Label(input_frame, text="传输模式: ").grid(row=0, column=7, sticky=tk.W, pady=5)
        self.transmitted_mode_var = tk.StringVar(value="未检测")
        self.transmitted_mode_label = ttk.Label(
            input_frame,
            textvariable=self.transmitted_mode_var,
            foreground="blue",
            width=10
        )
        self.transmitted_mode_label.grid(row=0, column=8, sticky=tk.W, pady=5)
        
        # 添加6个水平排列的空白备选框
        self.alternative_entries = []
        # 创建一个新行用于放置备选框
        ttk.Label(input_frame, text="备选框:").grid(row=1, column=0, sticky=tk.W, pady=5)
        for i in range(6):
            entry = ttk.Entry(input_frame, width=10)
            entry.grid(row=1, column=i+1, padx=(1, 1), pady=5, sticky=tk.W)
            self.alternative_entries.append(entry)
        
        # 高级选项复选框（默认隐藏，登录失败时显示）
        self.show_advanced_var = tk.BooleanVar(value=False)
        self.show_advanced_checkbox = ttk.Checkbutton(
            input_frame,
            text="显示高级选项",
            variable=self.show_advanced_var,
            command=self.toggle_advanced_options
        )
        
        # 创建高级选项框架
        self.advanced_frame = ttk.Frame(input_frame)
        self.advanced_frame.grid(row=2, column=0, columnspan=7, sticky=tk.W, pady=5)
        
        # 用户名输入 - 在高级选项框架中
        ttk.Label(self.advanced_frame, text="用户名:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.username_entry = ttk.Entry(self.advanced_frame, width=20)
        self.username_entry.grid(row=0, column=1, sticky=tk.W, pady=5)
        self.username_entry.insert(0, "pi-star")  # 默认用户名
        
        # 密码输入 - 在高级选项框架中
        ttk.Label(self.advanced_frame, text="密码:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.password_entry = ttk.Entry(self.advanced_frame, width=20, show="*")
        self.password_entry.grid(row=1, column=1, sticky=tk.W, pady=5)
        self.password_entry.insert(0, "raspberry")  # 默认密码
        
        # 初始隐藏高级选项
        self.toggle_advanced_options()
    
    def create_log_frame(self):
        """创建日志显示区域"""
        log_frame = ttk.LabelFrame(self.main_frame, text="测试日志", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 创建滚动文本框用于显示日志
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=('SimHei', 10),
            bg='#f0f0f0',
            height=15  # 设置固定高度为15行
        )
        self.log_text.pack(fill=tk.X, expand=False)  # 仅填充宽度，不自动扩展高度
        self.log_text.config(state=tk.DISABLED)  # 设置为只读
    
    def create_records_table(self):
        """创建通话记录表格区域"""
        # 创建表格框架
        table_frame = ttk.LabelFrame(self.main_frame, text="通话记录", padding="10")
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # 创建Treeview组件作为表格
        columns = ('call_sign', 'time', 'duration', 'source', 'slot', 'ber')
        self.records_tree = ttk.Treeview(table_frame, columns=columns, show='headings')
        
        # 设置列标题和宽度
        self.records_tree.heading('call_sign', text='呼号')
        self.records_tree.heading('time', text='时间')
        self.records_tree.heading('duration', text='时长(秒)')
        self.records_tree.heading('source', text='来源')
        self.records_tree.heading('slot', text='时隙')
        self.records_tree.heading('ber', text='误码率')
        
        # 设置列宽度
        self.records_tree.column('call_sign', width=100, anchor=tk.CENTER)
        self.records_tree.column('time', width=120, anchor=tk.CENTER)
        self.records_tree.column('duration', width=80, anchor=tk.CENTER)
        self.records_tree.column('source', width=80, anchor=tk.CENTER)
        self.records_tree.column('slot', width=80, anchor=tk.CENTER)
        self.records_tree.column('ber', width=80, anchor=tk.CENTER)
        
        # 创建垂直滚动条
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.records_tree.yview)
        self.records_tree.configure(yscroll=scrollbar.set)
        
        # 放置表格和滚动条
        self.records_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 设置表格样式，支持中文
        style = ttk.Style()
        style.configure("Treeview.Heading", font=('SimHei', 10, 'bold'))
        style.configure("Treeview", font=('SimHei', 10), rowheight=25)
    
    def create_button_frame(self):
        """创建按钮区域"""        # 按钮已移至输入区域，保留此方法以保持兼容性        pass
    
    def append_log(self, message):
        """向日志文本框添加消息"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)  # 滚动到最后一行
        self.log_text.config(state=tk.DISABLED)
        
    def clear_log(self):
        """清除日志文本框"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        
    def toggle_advanced_options(self):
        """切换高级选项的显示状态"""
        if self.show_advanced_var.get():
            self.advanced_frame.grid()  # 显示高级选项
        else:
            self.advanced_frame.grid_remove()  # 隐藏高级选项
            
    def disconnect(self):
        """断开与PI-STAR盒子的连接"""
        # 更新连接状态为未连接
        self.connection_status_var.set("未连接")
        self.connection_status_label.config(foreground="red")
        
        # 更新按钮文本和功能
        self.connect_button.config(
            text="连接",
            command=self.connect_test,
            state=tk.NORMAL
        )
        
        self.append_log("[INFO] 已断开连接")
        
    def connect_test(self):
        """测试与PI-STAR盒子的连接并登录"""
        # 获取输入值
        host = self.host_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        
        # 验证输入
        if not host:
            self.append_log("[ERROR] 请输入盒子地址")
            return
        
        # 禁用连接按钮，防止重复点击
        self.connect_button.config(state=tk.DISABLED)
        
        # 更新状态显示为"连接中..."
        self.connection_status_var.set("连接中...")
        self.connection_status_label.config(foreground="orange")
        
        # 创建测试器
        tester = PiStarHttpTester(
            host=host,
            username=username,
            password=password,
            timeout=10  # 固定超时时间为10秒
        )
        
        # 设置日志回调函数
        tester.set_log_callback(self.append_log)
        
        # 在新线程中运行连接测试，避免UI卡顿
        def connect_thread_func():
            try:
                # 测试连接
                self.append_log(f"[INFO] 开始抓取连接到 {host}")
                connect_success = tester.test_connection()
                
                if connect_success:
                    # 连接成功后尝试登录
                    self.append_log(f"[INFO] 开始尝试登录，用户名: {username}")
                    login_success = tester.login()
                    
                    # 根据登录结果更新连接状态
                    if login_success:
                        status_text = "已连接并登录"
                        status_color = "green"
                        self.append_log("[INFO] 登录成功！")
                        # 登录成功后隐藏高级选项复选框
                        self.root.after(0, lambda: self.show_advanced_checkbox.grid_remove())
                    else:
                        status_text = "已连接未登录"
                        status_color = "orange"
                        self.append_log("[INFO] 连接成功，但登录失败！")
                        # 登录失败时显示高级选项复选框
                        self.root.after(0, lambda: self.show_advanced_checkbox.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5))
                        # 登录失败时自动显示高级选项内容
                        self.root.after(0, lambda: self.show_advanced_var.set(True))
                        self.root.after(0, self.toggle_advanced_options)
                    
                    # 在主线程中更新UI，将按钮更改为断开按钮
                    self.root.after(0, lambda: self.connection_status_var.set(status_text))
                    self.root.after(0, lambda: self.connection_status_label.config(foreground=status_color))
                    self.root.after(0, lambda: self.connect_button.config(
                        text="断开",
                        command=self.disconnect,
                        state=tk.NORMAL
                    ))
                else:
                    # 连接失败
                    status_text = "连接失败"
                    status_color = "red"
                    self.append_log("[INFO] 连接失败！")
                    # 连接失败时隐藏高级选项复选框
                    self.root.after(0, lambda: self.show_advanced_checkbox.grid_remove())
                    
                    # 在主线程中更新UI，保持按钮为连接状态
                    self.root.after(0, lambda: self.connection_status_var.set(status_text))
                    self.root.after(0, lambda: self.connection_status_label.config(foreground=status_color))
                    self.root.after(0, lambda: self.connect_button.config(
                        text="连接",
                        command=self.connect_test,
                        state=tk.NORMAL
                    ))
            except Exception as e:
                # 处理异常情况
                self.root.after(0, lambda: self.connection_status_var.set("连接异常"))
                self.root.after(0, lambda: self.connection_status_label.config(foreground="red"))
                self.root.after(0, lambda: self.connect_button.config(
                    text="连接",
                    command=self.connect_test,
                    state=tk.NORMAL
                ))
                self.append_log(f"[ERROR] 连接过程中发生异常: {str(e)}")
            finally:
                # 确保按钮状态正确
                if not self.connect_button['state'] == tk.NORMAL:
                    self.root.after(0, lambda: self.connect_button.config(state=tk.NORMAL))
        
        # 启动连接测试线程
        thread = threading.Thread(target=connect_thread_func)
        thread.daemon = True  # 设置为守护线程，主程序退出时自动终止
        thread.start()
    
    def run_test(self):
        """运行测试"""
        # 获取输入值
        host = self.host_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        
        # 验证输入
        if not host:
            self.append_log("[ERROR] 请输入盒子地址")
            return
        
        # 禁用测试按钮，防止重复点击
        self.test_button.config(state=tk.DISABLED)
        
        # 创建测试器并运行测试
        tester = PiStarHttpTester(
            host=host,
            username=username,
            password=password,
            timeout=10  # 固定超时时间为10秒
        )
        
        # 设置日志回调函数
        tester.set_log_callback(self.append_log)
        
        # 检查当前是否已连接并登录，如果是，则跳过登录步骤
        current_status = self.connection_status_var.get()
        if "已连接并登录" in current_status:
            # 已经登录成功，跳过登录步骤直接获取状态信息
            def test_thread():
                try:
                    # 只获取状态信息，跳过连接和登录
                    tester._log('info', f"===== PI-STAR HTTP测试开始 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) =====")
                    # 由于是新创建的tester实例，需要重新设置认证信息和登录状态标志
                    tester.session.auth = (username, password)
                    tester.is_logged_in = True
                    status_info = tester.get_status_info()
                    if status_info:
                        tester._log('info', "测试成功：已成功获取PI-STAR盒子状态信息")
                        
                        # 将抓取的呼号和时隙信息按先后顺序填入备选框
                        if isinstance(status_info, list):
                            # 提取呼号和时隙信息
                            records_with_slot = []
                            for record in status_info:
                                call_sign = record['call_sign']
                                slot = record.get('slot', '')
                                records_with_slot.append((call_sign, slot))
                            
                            # 去除连续重复的记录，只保留一个
                            unique_records = []
                            for call_sign, slot in records_with_slot:
                                if not unique_records or (call_sign, slot) != unique_records[-1]:
                                    unique_records.append((call_sign, slot))
                            
                            # 获取当前选中的时隙值
                            selected_slot = self.timeslot_var.get()
                            
                            # 根据选中的时隙过滤记录
                            filtered_records = []
                            for call_sign, slot in unique_records:
                                # 检查时隙是否匹配或为空（空时隙表示不需要过滤）
                                if not slot or (selected_slot == 1 and '时隙1' in slot) or (selected_slot == 2 and '时隙2' in slot):
                                    filtered_records.append((call_sign, slot))
                            
                            # 在主线程中更新UI
                            def update_ui(filtered_records=filtered_records, tester=tester):
                                # 清空所有备选框
                                for entry in self.alternative_entries:
                                    entry.delete(0, tk.END)
                                
                                # 将过滤后的记录按顺序填入备选框
                                for i, (call_sign, slot) in enumerate(filtered_records):
                                    if i < len(self.alternative_entries):
                                        self.alternative_entries[i].delete(0, tk.END)
                                        # 正常显示呼号
                                        display_text = call_sign
                                        self.alternative_entries[i].insert(0, display_text)
                                # 更新传输模式显示
                                if hasattr(tester, 'transmitted_mode') and tester.transmitted_mode:
                                    self.transmitted_mode_var.set(tester.transmitted_mode)
                            
                            self.root.after(0, update_ui)
                finally:
                    # 测试结束后，重新启用测试按钮
                    self.root.after(0, lambda: self.test_button.config(state=tk.NORMAL))
        else:
            # 未登录或连接状态未知，运行完整测试流程
            def test_thread():
                try:
                    tester.run_full_test()
                    # 检查是否获取了状态信息
                    if hasattr(tester, '_last_status_info') and tester._last_status_info:
                        # 将抓取的呼号和时隙信息按先后顺序填入备选框
                        if isinstance(tester._last_status_info, list):
                            # 提取呼号和时隙信息
                            records_with_slot = []
                            for record in tester._last_status_info:
                                call_sign = record['call_sign']
                                slot = record.get('slot', '')
                                records_with_slot.append((call_sign, slot))
                            
                            # 去除连续重复的记录，只保留一个
                            unique_records = []
                            for call_sign, slot in records_with_slot:
                                if not unique_records or (call_sign, slot) != unique_records[-1]:
                                    unique_records.append((call_sign, slot))
                            
                            # 获取当前选中的时隙值
                            selected_slot = self.timeslot_var.get()
                            
                            # 根据选中的时隙过滤记录
                            filtered_records = []
                            for call_sign, slot in unique_records:
                                # 检查时隙是否匹配或为空（空时隙表示不需要过滤）
                                if not slot or (selected_slot == 1 and '时隙1' in slot) or (selected_slot == 2 and '时隙2' in slot):
                                    filtered_records.append((call_sign, slot))
                            
                            # 在主线程中更新UI
                            def update_ui(filtered_records=filtered_records, tester=tester):
                                # 清空所有备选框
                                for entry in self.alternative_entries:
                                    entry.delete(0, tk.END)
                                
                                # 将过滤后的记录按顺序填入备选框
                                for i, (call_sign, slot) in enumerate(filtered_records):
                                    if i < len(self.alternative_entries):
                                        self.alternative_entries[i].delete(0, tk.END)
                                        # 正常显示呼号
                                        display_text = call_sign
                                        self.alternative_entries[i].insert(0, display_text)
                                # 更新传输模式显示
                                if hasattr(tester, 'transmitted_mode') and tester.transmitted_mode:
                                    self.transmitted_mode_var.set(tester.transmitted_mode)
                            
                            self.root.after(0, update_ui)
                finally:
                    # 测试结束后，重新启用测试按钮
                    self.root.after(0, lambda: self.test_button.config(state=tk.NORMAL))
        
        # 启动测试线程
        thread = threading.Thread(target=test_thread)
        thread.daemon = True  # 设置为守护线程，主程序退出时自动终止
        thread.start()
        
    def toggle_continuous_test(self):
        """切换连续测试模式"""
        if self.is_continuous_testing:
            self.stop_continuous_test()
        else:
            self.start_continuous_test()
            
    def start_continuous_test(self):
        """开始连续测试"""
        # 获取输入值
        host = self.host_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        
        # 验证输入
        if not host:
            self.append_log("[ERROR] 请输入盒子地址")
            return
        
        # 更新状态
        self.is_continuous_testing = True
        self.continuous_test_stop_event.clear()
        
        # 更新按钮状态
        self.test_button.config(state=tk.DISABLED)
        self.lock_test_button.config(text="停止测试")
        
        self.append_log("[INFO] 开始连续测试模式，按'停止测试'按钮结束")
        
        # 创建测试器
        tester = PiStarHttpTester(
            host=host,
            username=username,
            password=password,
            timeout=10  # 固定超时时间为10秒
        )
        
        # 设置日志回调函数
        tester.set_log_callback(self.append_log)
        
        # 连续测试线程函数
        def continuous_test_thread_func():
            try:
                while not self.continuous_test_stop_event.is_set():
                    self.append_log("\n[INFO] ===== 开始新一轮测试 =====")
                    tester.run_full_test()
                    
                    # 检查是否需要停止
                    if self.continuous_test_stop_event.is_set():
                        break
                    
                    # 间隔5秒后进行下一次测试
                    self.append_log("[INFO] 等待5秒后进行下一次测试...")
                    self.continuous_test_stop_event.wait(5)
            finally:
                # 测试结束后，恢复按钮状态
                self.root.after(0, lambda: self._reset_test_buttons())
                self.append_log("[INFO] 连续测试已停止")
        
        # 启动连续测试线程
        self.continuous_test_thread = threading.Thread(target=continuous_test_thread_func)
        self.continuous_test_thread.daemon = True  # 设置为守护线程，主程序退出时自动终止
        self.continuous_test_thread.start()
        
    def stop_continuous_test(self):
        """停止连续测试"""
        self.continuous_test_stop_event.set()
        self.is_continuous_testing = False
        self.lock_test_button.config(text="锁定测试")
        self.append_log("[INFO] 正在停止连续测试...")
        
    def _reset_test_buttons(self):
        """重置测试按钮状态"""
        self.test_button.config(state=tk.NORMAL)
        self.lock_test_button.config(text="锁定测试")
        self.is_continuous_testing = False

def main():
    """主函数，处理命令行参数并执行测试"""
    # 检查是否有命令行参数
    if len(sys.argv) > 1:
        # 命令行模式
        # 解析命令行参数
        parser = argparse.ArgumentParser(description='PI-STAR HTTP连接测试工具')
        parser.add_argument('host', help='盒子的IP地址或主机名')
        parser.add_argument('--username', '-u', default='pi-star', help='登录用户名（默认为pi-star）')
        parser.add_argument('--password', '-p', default='raspberry', help='登录密码（默认为raspberry）')
        
        args = parser.parse_args()
        
        # 创建测试器并运行测试
        tester = PiStarHttpTester(
            host=args.host,
            username=args.username,
            password=args.password,
            timeout=10  # 固定超时时间为10秒
        )
        
        success = tester.run_full_test()
        
        # 根据测试结果设置退出码
        sys.exit(0 if success else 1)
    else:
        # GUI模式
        root = tk.Tk()
        app = PiStarHttpTestApp(root)
        root.mainloop()

if __name__ == "__main__":
    main()