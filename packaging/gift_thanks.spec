# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：礼物答谢姬（onedir 模式）。

用法（在仓库根目录执行）：
    pyinstaller packaging/gift_thanks.spec --noconfirm

前置条件：
    1. pip install -r requirements.txt pyinstaller
    2. 字体已下载到 gift_thanks/templates/fonts/NotoSansSC-Regular.otf
    3. set PLAYWRIGHT_BROWSERS_PATH=0 && playwright install chromium
       （浏览器装进 playwright/driver/package/.local-browsers，
         由下面的 collect_data_files('playwright') 一并打包）

产物：dist/GiftThanks/GiftThanks.exe（连同所有依赖的目录，交给 Inno Setup 打包）
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 文件在 packaging/ 下，仓库根目录取其上一级（与 pyinstaller 调用目录无关）
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

block_cipher = None

# ---- 数据文件 ----
# 模板（HTML + 字体）与默认头像：保持包内目录结构，代码里用相对 __file__ 定位
datas = [
    (os.path.join(ROOT, "gift_thanks", "templates"), os.path.join("gift_thanks", "templates")),
    (os.path.join(ROOT, "gift_thanks", "assets"), os.path.join("gift_thanks", "assets")),
]
# playwright 驱动（node.exe / driver 脚本）与 .local-browsers 里的 chromium
datas += collect_data_files("playwright")

# ---- 隐式导入 ----
hiddenimports = [
    "blivedm",
    "qrcode",
    "cryptography",
    "jinja2",
]
hiddenimports += collect_submodules("playwright")   # playwright.sync_api 等
hiddenimports += collect_submodules("blivedm")      # blivedm 各 handler/client 子模块
hiddenimports += collect_submodules("cryptography") # Fernet 依赖的后端模块


a = Analysis(
    [os.path.join(ROOT, "gift_thanks", "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 砍掉用不到的大块头，缩小安装包体积
        "tkinter", "matplotlib", "numpy", "pandas", "scipy",
        "PyQt5", "PyQt6", "PySide2",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir：二进制交给 COLLECT
    name="GiftThanks",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 容易触发杀软误报，不用
    console=False,           # 无黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GiftThanks",
)
