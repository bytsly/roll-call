#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创建客户端升级包脚本
用于生成服务端给用户升级客户端的压缩包
"""

import os
import sys
import zipfile
import hashlib
import json
from datetime import datetime

def create_update_package():
    """创建客户端升级包"""
    
    # 检查dist目录是否存在EXE文件
    exe_path = os.path.join('dist', 'roll_call_app.exe')
    if not os.path.exists(exe_path):
        print("错误：未找到编译后的EXE文件，请先运行PyInstaller")
        return False
    
    # 创建updates目录
    updates_dir = 'updates'
    os.makedirs(updates_dir, exist_ok=True)
    
    # 计算文件哈希值
    with open(exe_path, 'rb') as f:
        file_hash = hashlib.md5(f.read()).hexdigest()
    
    # 获取文件大小和修改时间
    file_size = os.path.getsize(exe_path)
    file_mtime = datetime.fromtimestamp(os.path.getmtime(exe_path))
    
    # 版本信息（可以从配置文件读取或自动生成）
    version = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 创建版本信息文件
    version_info = {
        "version": version,
        "build_date": file_mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "file_size": file_size,
        "file_hash": file_hash,
        "file_name": "roll_call_app.exe",
        "description": "点名系统客户端更新包"
    }
    
    # 写入版本信息文件
    version_file = os.path.join(updates_dir, f"version_{version}.json")
    with open(version_file, 'w', encoding='utf-8') as f:
        json.dump(version_info, f, indent=2, ensure_ascii=False)
    
    # 创建ZIP升级包
    zip_filename = os.path.join(updates_dir, f"roll_call_app_update_{version}.zip")
    
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # 添加EXE文件
        zipf.write(exe_path, "roll_call_app.exe")
        
        # 添加版本信息文件
        zipf.write(version_file, "version.json")
        
        # 添加必要的配置文件（如果存在）
        config_files = ['app_config.ini', 'database_version.txt']
        for config_file in config_files:
            if os.path.exists(config_file):
                zipf.write(config_file, config_file)
    
    print(f"升级包创建成功: {zip_filename}")
    print(f"版本: {version}")
    print(f"文件大小: {file_size} bytes")
    print(f"MD5哈希: {file_hash}")
    
    # 清理临时版本文件
    os.remove(version_file)
    
    return True

def create_server_update_script():
    """创建服务端更新脚本"""
    
    script_content = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名系统服务端更新脚本
用于自动检测和部署客户端更新
"""

import os
import sys
import json
import hashlib
import zipfile
import shutil
from datetime import datetime

class ClientUpdater:
    def __init__(self):
        self.update_dir = 'updates'
        self.client_dir = 'download'  # 客户端文件存放目录
        self.current_version_file = os.path.join(self.client_dir, 'current_version.json')
        
    def get_current_version(self):
        """获取当前客户端版本"""
        if os.path.exists(self.current_version_file):
            with open(self.current_version_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None
    
    def check_for_updates(self):
        """检查是否有新版本可用"""
        current = self.get_current_version()
        if not current:
            return None  # 没有当前版本信息
        
        # 查找最新的更新包
        update_files = []
        for file in os.listdir(self.update_dir):
            if file.startswith('roll_call_app_update_') and file.endswith('.zip'):
                update_files.append(file)
        
        if not update_files:
            return None  # 没有更新包
        
        # 按文件名排序（版本号在文件名中）
        update_files.sort(reverse=True)
        latest_update = update_files[0]
        
        # 提取版本号
        version = latest_update.replace('roll_call_app_update_', '').replace('.zip', '')
        
        # 如果新版本比当前版本新
        if version > current.get('version', ''):
            return {
                'update_file': latest_update,
                'version': version,
                'path': os.path.join(self.update_dir, latest_update)
            }
        
        return None
    
    def apply_update(self, update_info):
        """应用更新"""
        update_file = update_info['path']
        
        # 创建临时目录
        temp_dir = 'temp_update'
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # 解压更新包
            with zipfile.ZipFile(update_file, 'r') as zipf:
                zipf.extractall(temp_dir)
            
            # 验证文件完整性
            version_file = os.path.join(temp_dir, 'version.json')
            if not os.path.exists(version_file):
                raise Exception("更新包中缺少版本信息文件")
            
            with open(version_file, 'r', encoding='utf-8') as f:
                version_info = json.load(f)
            
            # 验证EXE文件哈希
            exe_path = os.path.join(temp_dir, 'roll_call_app.exe')
            if not os.path.exists(exe_path):
                raise Exception("更新包中缺少EXE文件")
            
            with open(exe_path, 'rb') as f:
                file_hash = hashlib.md5(f.read()).hexdigest()
            
            if file_hash != version_info['file_hash']:
                raise Exception("文件哈希不匹配，更新包可能已损坏")
            
            # 备份当前版本
            backup_dir = os.path.join(self.client_dir, 'backup')
            os.makedirs(backup_dir, exist_ok=True)
            
            current_exe = os.path.join(self.client_dir, 'roll_call_app.exe')
            if os.path.exists(current_exe):
                backup_file = os.path.join(backup_dir, f"roll_call_app_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.exe")
                shutil.copy2(current_exe, backup_file)
            
            # 部署新版本
            shutil.copy2(exe_path, current_exe)
            
            # 更新版本信息
            with open(self.current_version_file, 'w', encoding='utf-8') as f:
                json.dump(version_info, f, indent=2, ensure_ascii=False)
            
            # 清理临时文件
            shutil.rmtree(temp_dir)
            
            return True
            
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            raise e
    
    def run(self):
        """运行更新检查"""
        try:
            update = self.check_for_updates()
            if update:
                print(f"发现新版本: {update['version']}")
                print("开始应用更新...")
                self.apply_update(update)
                print("更新完成!")
            else:
                print("当前已是最新版本")
        except Exception as e:
            print(f"更新失败: {str(e)}")

if __name__ == "__main__":
    updater = ClientUpdater()
    updater.run()
'''
    
    with open('server_update_client.py', 'w', encoding='utf-8') as f:
        f.write(script_content)
    
    print("服务端更新脚本已创建: server_update_client.py")

if __name__ == "__main__":
    # 创建升级包
    if create_update_package():
        # 创建服务端更新脚本
        create_server_update_script()
        print("\\n升级包和相关脚本创建完成!")
        print("请将updates目录下的ZIP文件上传到服务端")
        print("服务端可以使用server_update_client.py脚本自动部署更新")
    else:
        print("升级包创建失败，请检查错误信息")