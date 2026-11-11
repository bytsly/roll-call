#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终打包脚本 - 创建完整的exe文件
包含所有依赖、图标、配置文件和资源
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# 导入Git工具
try:
    from git_utils import GitUtils, AutoGitContext
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False
    print("⚠️ Git工具不可用，跳过自动提交")

# 简单的空上下文管理器
class nullcontext:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

def main():
    # 使用自动Git上下文管理器
    with AutoGitContext("打包exe文件") if GIT_AVAILABLE else nullcontext():
        print("=" * 60)
        print("点名软件完整版打包工具")
        print("=" * 60)
    
    # 当前目录
    current_dir = Path.cwd()
    print(f"当前工作目录: {current_dir}")
    
    # 检查必要文件
    required_files = [
        'roll_call_app.py',
        'app_config.ini', 
        'CRAC.ico',
        'city_data.pkl',
        'tools/',
        'requirements.txt'
    ]
    
    print("\n📋 检查必要文件:")
    for file_path in required_files:
        full_path = current_dir / file_path
        if full_path.exists():
            print(f"   ✓ {file_path}")
        else:
            print(f"   ✗ {file_path} - 缺失!")
            return False
    
    # 安装依赖
    print("\n📦 安装依赖包...")
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'], 
                      check=True, capture_output=True)
        print("   ✓ 依赖安装完成")
    except subprocess.CalledProcessError as e:
        print(f"   ✗ 依赖安装失败: {e}")
        return False
    
    # 清理之前的构建
    print("\n🧹 清理之前的构建...")
    for cleanup_dir in ['build', 'dist']:
        cleanup_path = current_dir / cleanup_dir
        if cleanup_path.exists():
            shutil.rmtree(cleanup_path)
            print(f"   ✓ 已清理 {cleanup_dir}")
    
    # 创建优化的spec文件
    spec_content = '''# -*- mode: python ; coding: utf-8 -*-

# 分析阶段
a = Analysis(
    ['roll_call_app.py'],
    pathex=[''' + f"'{current_dir}'" + '''],
    binaries=[],
    datas=[
        ('app_config.ini', '.'),
        ('city_data.pkl', '.'),
        ('CRAC.ico', '.'),
        ('tools', 'tools'),
    ],
    hiddenimports=[
        'PIL._tkinter_finder',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'sqlite3',
        'requests',
        'reportlab',
        'reportlab.lib.pagesizes',
        'reportlab.lib.styles',
        'reportlab.platypus',
        'reportlab.lib.colors',
        'reportlab.pdfbase',
        'reportlab.pdfbase.ttfonts',
        'docx',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'matplotlib',
        'numpy',
        'pandas',
        'pickle',
        'configparser',
        'threading',
        'datetime',
        're',
        'csv',
        'hashlib',
        'queue',
        'tempfile',
        'ctypes',
        'shutil',
        'os'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib.tests',
        'numpy.tests',
        'pandas.tests',
        'unittest',
        'test',
        'tests'
    ],
    noarchive=False,
    optimize=2,
)

# PYZ阶段
pyz = PYZ(a.pure)

# EXE阶段
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='点名软件_完整版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['CRAC.ico'],
    version='version_info.txt' if Path('version_info.txt').exists() else None,
)
'''
    
    # 写入spec文件
    spec_file = current_dir / '点名软件_最终版.spec'
    with open(spec_file, 'w', encoding='utf-8') as f:
        f.write(spec_content)
    print(f"   ✓ 已创建优化的spec文件: {spec_file}")
    
    # 执行打包
    print("\n🔨 开始打包...")
    try:
        result = subprocess.run([
            sys.executable, '-m', 'PyInstaller', 
            str(spec_file),
            '--clean',
            '--noconfirm'
        ], check=True, capture_output=True, text=True)
        
        print("   ✓ 打包完成!")
        
    except subprocess.CalledProcessError as e:
        print(f"   ✗ 打包失败: {e}")
        print(f"   错误输出: {e.stderr}")
        return False
    
    # 检查生成的文件
    dist_dir = current_dir / 'dist'
    exe_file = dist_dir / '点名软件_完整版.exe'
    
    print(f"\n📊 打包结果:")
    if exe_file.exists():
        file_size = exe_file.stat().st_size / (1024 * 1024)  # MB
        print(f"   ✓ exe文件生成成功")
        print(f"   文件位置: {exe_file}")
        print(f"   文件大小: {file_size:.1f} MB")
        
        # 创建便携版文件夹
        portable_dir = current_dir / '点名软件_便携版'
        if portable_dir.exists():
            shutil.rmtree(portable_dir)
        
        portable_dir.mkdir()
        shutil.copy2(exe_file, portable_dir)
        
        # 复制必要的数据文件到便携版
        data_files = [
            'app_config.ini',
            'city_data.pkl'
        ]
        
        for data_file in data_files:
            src = current_dir / data_file
            dst = portable_dir / data_file
            if src.exists():
                shutil.copy2(src, dst)
                print(f"   ✓ 已复制 {data_file}")
        
        # 创建说明文件
        readme_content = '''点名软件_完整版 使用说明
========================

1. 运行环境:
   - Windows 7/8/10/11 64位系统
   - 无需安装Python环境
   - 包含所有必要的依赖库

2. 使用方法:
   - 双击 "点名软件_完整版.exe" 启动程序
   - 首次运行会自动创建数据库文件
   - 支持所有原有功能

3. 功能特性:
   - PI-STAR盒子通话记录抓取
   - 呼号数据库管理
   - 多种导出格式(PDF/Word/Excel)
   - 快捷键操作
   - 历史记录查询

4. 文件说明:
   - 点名软件_完整版.exe: 主程序
   - app_config.ini: 配置文件
   - city_data.pkl: 城市数据
   - *.db: 数据库文件(运行时生成)

5. 注意事项:
   - 请保持所有文件在同一目录
   - 首次运行可能需要几秒钟加载时间
   - 如遇到杀毒软件误报，请添加信任

版本: 1.11
更新日期: 2025-11-26
'''
        
        with open(portable_dir / '使用说明.txt', 'w', encoding='utf-8') as f:
            f.write(readme_content)
        
        print(f"   ✓ 便携版已创建: {portable_dir}")
        
    else:
        print(f"   ✗ exe文件未生成")
        return False
    
    print(f"\n🎉 打包完成!")
    print(f"   主程序: {exe_file}")
    print(f"   便携版: {portable_dir}")
    
    return True

if __name__ == '__main__':
    try:
        success = main()
        if success:
            print("\n✅ 所有操作完成!")
        else:
            print("\n❌ 打包过程中出现错误!")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断操作")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 未预期的错误: {e}")
        sys.exit(1)