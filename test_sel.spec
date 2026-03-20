# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['test_sel.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['selenium', 'selenium.webdriver', 'selenium.webdriver.edge', 'selenium.webdriver.edge.webdriver', 'selenium.webdriver.edge.options', 'selenium.webdriver.edge.service', 'selenium.webdriver.remote', 'selenium.webdriver.remote.webdriver', 'selenium.webdriver.chromium', 'selenium.webdriver.chromium.webdriver', 'selenium.webdriver.chromium.options', 'selenium.webdriver.chromium.service'],
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
    name='test_sel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
