# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['bubblegum_app.py'],
    pathex=[],
    binaries=[],
    datas=[('bubblegum.html', '.'), ('bubblegum.ico', '.')],
    hiddenimports=['selenium', 'selenium.webdriver', 'selenium.webdriver.edge', 'selenium.webdriver.edge.webdriver', 'selenium.webdriver.edge.options', 'selenium.webdriver.edge.service', 'selenium.webdriver.common', 'selenium.webdriver.common.by', 'selenium.webdriver.support', 'selenium.webdriver.support.ui', 'selenium.webdriver.support.expected_conditions', 'selenium.webdriver.remote', 'selenium.webdriver.remote.webdriver', 'selenium.webdriver.chromium', 'selenium.webdriver.chromium.webdriver', 'selenium.webdriver.chromium.options', 'selenium.webdriver.chromium.service'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BubbleGum',
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
    icon=['bubblegum.ico'],
)
