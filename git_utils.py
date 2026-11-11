#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Git工具模块 - 提供自动Git提交功能
可以集成到其他脚本中使用
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

class GitUtils:
    @staticmethod
    def is_git_repo(path="."):
        """检查是否为git仓库"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                cwd=path,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except:
            return False
    
    @staticmethod
    def has_changes(path="."):
        """检查是否有未提交的更改"""
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=path,
                capture_output=True,
                text=True
            )
            return len(result.stdout.strip()) > 0
        except:
            return False
    
    @staticmethod
    def auto_commit(message=None, path="."):
        """自动提交更改"""
        if not GitUtils.is_git_repo(path):
            return False, "不是Git仓库"
        
        if not GitUtils.has_changes(path):
            return True, "没有需要提交的更改"
        
        try:
            # 添加所有更改
            subprocess.run(
                ['git', 'add', '.'],
                cwd=path,
                check=True,
                capture_output=True
            )
            
            # 生成提交消息
            if not message:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = f"自动提交 - {timestamp}"
            
            # 提交更改
            subprocess.run(
                ['git', 'commit', '-m', message],
                cwd=path,
                check=True,
                capture_output=True
            )
            
            return True, "提交成功"
            
        except subprocess.CalledProcessError as e:
            return False, f"提交失败: {e}"
    
    @staticmethod
    def smart_commit(operation_type="更新", path="."):
        """智能提交 - 根据操作类型生成提交消息"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        message = f"{operation_type} - {timestamp}"
        return GitUtils.auto_commit(message, path)

# 装饰器函数，用于自动提交
def auto_git_commit(operation_name="操作"):
    """装饰器：在函数执行后自动提交Git更改"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            # 执行原函数
            result = func(*args, **kwargs)
            
            # 自动提交Git更改
            success, message = GitUtils.smart_commit(operation_name)
            if success:
                print(f"✅ {message}")
            else:
                print(f"⚠️ {message}")
            
            return result
        return wrapper
    return decorator

# 上下文管理器，用于自动提交
class AutoGitContext:
    def __init__(self, operation_name="操作", path="."):
        self.operation_name = operation_name
        self.path = path
        self.changes_before = False
    
    def __enter__(self):
        self.changes_before = GitUtils.has_changes(self.path)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:  # 没有异常发生
            success, message = GitUtils.smart_commit(self.operation_name, self.path)
            if success:
                print(f"✅ {message}")
            else:
                print(f"⚠️ {message}")
        else:
            print(f"❌ 操作失败，跳过Git提交: {exc_val}")

if __name__ == '__main__':
    # 测试代码
    if GitUtils.is_git_repo():
        success, message = GitUtils.auto_commit("测试提交")
        print(f"结果: {message}")
    else:
        print("当前目录不是Git仓库")