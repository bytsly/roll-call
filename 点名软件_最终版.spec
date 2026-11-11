# -*- mode: python ; coding: utf-8 -*-

# 分析阶段
a = Analysis(
    ['roll_call_app.py'],
    pathex=['D:\\soft\\点名软件'],
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
    version=None,
)
