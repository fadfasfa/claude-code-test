# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['hextech_ui.py'],  # 这里是我们系统的绝对主入口
    pathex=[],
    binaries=[],
    datas=[],
    # 【核心防御】：强制挂载所有隐式依赖，防止在别的电脑上闪退
    hiddenimports=[
        'pandas',
        'requests',
        'PIL',
        'PIL._tkinter_finder',
        'win32gui',
        'win32con',
        'psutil'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Hextech伴生终端',    # 生成的 EXE 文件名
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                # 开启 UPX 压缩（如果已安装），减小体积
    upx_exclude=[],
    runtime_tmpdir=None,
    # 【生命线】：必须保持为 True！否则您的 1、2、3 快捷查询和拼音检索将直接报 EOFError 崩溃
    console=True,            
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='NONE'              # 如果您有专属的 .ico 图标，可以把 'NONE' 改为 '你的图标名.ico'
)