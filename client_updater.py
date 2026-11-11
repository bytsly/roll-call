"""
客户端更新模块
从MMDVM Updater中提取的独立更新功能，可以集成到其他Python程序中
"""

import os
import sys
import re
import requests
import json
import threading
import shutil
import subprocess
import tkinter as tk
from tkinter import messagebox


class ClientUpdater:
    """客户端自动更新类"""
    
    def __init__(self, current_version, update_server_urls=None, root_window=None):
        """
        初始化更新器
        
        参数:
            current_version: 当前程序版本号
            update_server_urls: 更新服务器URL列表，默认为None时使用默认URL
            root_window: Tkinter根窗口，用于显示UI元素
        """
        # 当前程序版本
        self.VERSION = current_version
        
        # 更新服务器URL列表
        if update_server_urls is None:
            # 默认更新服务器URL
            self.UPDATE_SERVER_URLS = [
                "https://example.com/update/mmdvm_updater.json",
                "https://backup.example.com/update/mmdvm_updater.json"
            ]
        else:
            self.UPDATE_SERVER_URLS = update_server_urls
        
        # Tkinter根窗口
        self.root = root_window
        
        # 进度变量
        self.progress_var = tk.DoubleVar(value=0) if root_window else None
        self.progress_label = None
        self.current_file = ""
        self.is_resuming = False
        self.resume_file = ""
        
        # 日志函数引用
        self._log_func = None
    
    def set_log_function(self, log_func):
        """
        设置日志函数，用于记录更新过程中的消息
        
        参数:
            log_func: 日志函数，接收一个字符串参数
        """
        self._log_func = log_func
    
    def set_progress_ui(self, progress_var, progress_label):
        """
        设置进度条UI组件
        
        参数:
            progress_var: tk.DoubleVar，用于控制进度条
            progress_label: tk.Label，用于显示进度文本
        """
        self.progress_var = progress_var
        self.progress_label = progress_label
    
    def log(self, message):
        """
        记录日志
        
        参数:
            message: 日志消息
        """
        if self._log_func:
            self._log_func(message)
        else:
            # 默认输出到控制台
            print(message)
    
    def parse_version(self, version_str):
        """
        解析版本字符串，返回整数元组用于比较
        
        参数:
            version_str: 版本字符串
            
        返回:
            整数元组，如(1, 11)代表版本1.11
        """
        try:
            # 提取版本号数字部分，例如从"1.07"提取"1"和"07"
            parts = re.findall(r'\d+', version_str)
            # 转换为整数元组
            return tuple(int(part) for part in parts)
        except Exception:
            # 如果解析失败，返回一个非常小的版本号
            return (0, 0)
            
    def check_for_updates(self):
        """
        检查是否有新版本可用 - 适配点名软件更新接口
        
        返回:
            字典，包含更新信息或None（无更新或检查失败）
        """
        try:
            # 发送请求到更新服务器
            self.log(f"检查程序更新中...")
            
            # 按顺序尝试所有服务器
            update_info = None
            for server_url in self.UPDATE_SERVER_URLS:
                try:
                    # 点名软件的更新接口需要传递版本参数
                    params = {"version": self.VERSION}
                    self.log(f"尝试连接服务器: {server_url}")
                    response = requests.get(server_url, params=params, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    
                    # 适配点名软件的返回格式
                    if data.get("update_available"):
                        # 点名软件格式转换
                        update_info = {
                            "version": data.get("latest_version", ""),
                            "download_url": f"{server_url.replace('/check_update', '/download_update')}/{data.get('update_info', {}).get('package_file', '')}",
                            "release_notes": data.get("update_info", {}).get("description", "") + "\n" + "\n".join([f"- {c}" for c in data.get("update_info", {}).get("changes", [])]),
                            "has_update": True
                        }
                        break
                    else:
                        self.log(f"当前已是最新版本: v{self.VERSION}")
                        return None
                        
                except requests.exceptions.RequestException as e:
                    self.log(f"连接服务器失败: {str(e)}")
                    continue  # 尝试下一个服务器
            
            if not update_info:
                self.log(f"检查更新失败: 所有服务器均无法连接")
                return None
            
            # 比较版本号
            current_version_parts = self.parse_version(self.VERSION)
            latest_version_parts = self.parse_version(update_info.get('version', ''))
            
            # 如果有新版本可用
            if latest_version_parts > current_version_parts:
                return update_info
            else:
                self.log(f"当前已是最新版本: v{self.VERSION}")
                return None
                
        except requests.exceptions.RequestException:
            # 网络错误时静默失败，不影响程序使用
            self.log(f"检查更新失败: 网络连接问题")
        except Exception as e:
            self.log(f"检查更新失败: {str(e)}")
        
        return None
            
    def prompt_update(self, update_info):
        """
        提示用户有新版本可用
        
        参数:
            update_info: 更新信息字典
            
        返回:
            布尔值，用户是否确认更新
        """
        if not self.root:
            self.log("无法显示更新提示：未提供Tkinter根窗口")
            return False
        
        latest_version = update_info.get('version', '')
        download_url = update_info.get('download_url', '')
        release_notes = update_info.get('release_notes', '')
        
        # 构建更新消息
        message = f"发现新版本: v{latest_version}\n\n"
        if release_notes:
            message += f"更新内容:\n{release_notes}\n\n"
        message += "是否立即更新？"
        
        # 显示确认对话框
        return messagebox.askyesno("更新可用", message)
            
    def download_and_update(self, download_url):
        """
        下载新版本并更新程序
        
        参数:
            download_url: 更新包下载链接
            
        返回:
            布尔值，更新是否成功
        """
        try:
            self.log(f"开始下载更新...")
            
            # 导入需要的模块
            import requests
            import os
            import sys
            import subprocess
            import shutil
        
            # 创建up文件夹（如果不存在）
            current_exe = sys.executable
            current_dir = os.path.dirname(current_exe)
            up_dir = os.path.join(current_dir, "up")
            os.makedirs(up_dir, exist_ok=True)
            
            # 在up文件夹中下载更新包
            update_file_path = os.path.join(up_dir, "update.zip")
        
            # 下载文件并显示进度
            response = requests.get(download_url, stream=True, timeout=120)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(update_file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        # 更新进度（在UI线程中）
                        if total_size > 0 and self.root and self.progress_var:
                            progress = (downloaded_size / total_size) * 100
                            self.root.after(0, self.update_progress, progress)
            
            # 下载完成
            self.log(f"更新包下载完成...")
            
            # 再次获取当前程序路径，确保准确性
            current_exe = sys.executable
            current_dir = os.path.dirname(current_exe)
            
            # 将更新文件移动到软件根目录
            new_update_file_path = os.path.join(current_dir, "update.zip")
            
            # 如果文件已存在，先删除
            if os.path.exists(new_update_file_path):
                try:
                    os.remove(new_update_file_path)
                except Exception as e:
                    self.log(f"无法删除已存在的更新包: {e}")
            
            # 移动文件
            try:
                shutil.move(update_file_path, new_update_file_path)
                self.log(f"更新包已移动到软件根目录: {new_update_file_path}")
            except Exception as e:
                self.log(f"移动更新包失败: {e}")
                # 如果移动失败，使用原路径
                new_update_file_path = update_file_path
                
            # 创建批处理文件来提醒用户手动更新
            batch_file = os.path.join(current_dir, "update_reminder.bat")
            with open(batch_file, 'w', encoding='utf-8') as f:
                f.write(f'@echo off\n')
                f.write(f'chcp 65001 >nul\n')  # 设置UTF-8编码
                f.write(f'cls\n')
                f.write(f'echo ======================================================\n')
                f.write(f'echo              程序更新 - 更新提醒\n')
                f.write(f'echo ======================================================\n')
                f.write(f'echo  \n')
                f.write(f'echo  新版本更新包已下载完成！\n')
                f.write(f'echo  \n')
                f.write(f'echo  更新包位置：{new_update_file_path}\n')
                f.write(f'echo  \n')
                f.write(f'echo  请手动解压缩更新包到软件所在目录！\n')
                f.write(f'echo  \n')
                f.write(f'echo  解压缩完成后，直接运行程序即可使用新版本。\n')
                f.write(f'echo  \n')
                f.write(f'echo ======================================================\n')
                f.write(f'echo  \n')
                f.write(f'echo  按任意键关闭此窗口...\n')
                f.write(f'pause >nul\n')
                f.write(f'exit\n')
        
            # 通知用户更新情况
            if self.root:
                self.root.after(0, lambda: 
                    messagebox.showinfo("更新完成", f"更新包已下载到软件根目录\n\n请手动解压缩'{os.path.basename(new_update_file_path)}'后使用新版本。"))
            else:
                self.log(f"更新包已下载完成: {new_update_file_path}")
                self.log("请手动解压缩更新包到软件所在目录")
            
            # 启动提醒批处理文件
            subprocess.Popen([batch_file], shell=True)
                
            return True
            
        except Exception as e:
            # 替换错误信息中的域名链接、纯域名和IP地址为"服务器"
            import re
            error_msg = str(e)
            # 匹配HTTP/HTTPS URL
            modified_error_msg = re.sub(r'https?://[^\s]+', '服务器', error_msg)
            # 匹配纯域名(如example.com)
            modified_error_msg = re.sub(r'([a-zA-Z0-9][-a-zA-Z0-9]{0,62}(\.[a-zA-Z0-9][-a-zA-Z0-9]{0,62})+\.?)', '服务器', modified_error_msg)
            # 匹配IP地址
            modified_error_msg = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '服务器', modified_error_msg)
            final_error_msg = f"更新失败: {modified_error_msg}"
            
            if self.root:
                self.root.after(0, lambda: messagebox.showerror("更新错误", final_error_msg))
            self.log(final_error_msg)
            
            if self.root and self.progress_var:
                self.root.after(0, lambda: self.update_progress(0))
            
            return False
    
    def update_progress(self, value):
        """
        更新进度条和百分比显示
        
        参数:
            value: 进度值(0-100)
        """
        if self.progress_var:
            self.progress_var.set(value)
        
        # 更新百分比标签，精确到小数点后两位
        if self.progress_label:
            if hasattr(self, 'current_file') and self.current_file:
                if self.is_resuming:
                    self.progress_label.config(
                        text=f"{self.current_file} {value:.2f}% 断点续传"
                    )
                else:
                    self.progress_label.config(
                        text=f"{self.current_file} {value:.2f}%"
                    )
            else:
                if self.is_resuming and self.resume_file:
                    self.progress_label.config(
                        text=f"{self.resume_file} {value:.2f}% 断点续传"
                    )
                else:
                    self.progress_label.config(text=f"{value:.2f}%")
    
    def check_and_update(self):
        """
        检查更新并执行更新流程
        
        返回:
            布尔值，更新是否成功或已完成
        """
        # 检查更新
        update_info = self.check_for_updates()
        
        if update_info:
            # 提示用户
            if self.prompt_update(update_info):
                # 启动下载线程
                if self.root:
                    download_thread = threading.Thread(
                        target=self.download_and_update,
                        args=(update_info.get('download_url', ''),),
                        daemon=True
                    )
                    download_thread.start()
                    return True
                else:
                    # 无UI时直接下载
                    return self.download_and_update(update_info.get('download_url', ''))
            return False
        return False


# 使用示例
if __name__ == "__main__":
    # 创建简单的Tkinter窗口来演示更新功能
    root = tk.Tk()
    root.title("更新器演示")
    root.geometry("400x300")
    
    # 创建进度条
    progress_var = tk.DoubleVar(value=0)
    progress_bar = tk.ttk.Progressbar(root, variable=progress_var, length=300)
    progress_bar.pack(pady=20)
    
    # 创建进度标签
    progress_label = tk.Label(root, text="0.00%")
    progress_label.pack(pady=5)
    
    # 创建日志文本框
    log_text = tk.Text(root, height=10, width=50)
    log_text.pack(pady=10)
    
    # 自定义日志函数
    def log_message(message):
        log_text.insert(tk.END, message + "\n")
        log_text.see(tk.END)
    
    # 初始化更新器
    updater = ClientUpdater(
        current_version="1.00",  # 测试版本，确保低于远程版本
        root_window=root
    )
    updater.set_log_function(log_message)
    updater.set_progress_ui(progress_var, progress_label)
    
    # 创建检查更新按钮
    def on_check_update():
        log_message("开始检查更新...")
        threading.Thread(target=updater.check_and_update, daemon=True).start()
    
    check_button = tk.Button(root, text="检查更新", command=on_check_update)
    check_button.pack(pady=10)
    
    root.mainloop()