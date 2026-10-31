# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

构建：`build.bat` 或 `python -m PyInstaller VBHelper.spec`。

不要用命令行参数重新生成 spec —— `--specpath` 会把本文件覆盖成默认模板，
hiddenimports 和资源清单会一起丢失。
"""
from PyInstaller.utils.hooks import collect_data_files

# 资源：图标字体、程序图标、第三方许可文本都在 assets 下
datas = [('vbhelper/assets', 'vbhelper/assets')]
datas += collect_data_files('certifi')

hiddenimports = [
    # features 下的功能包由 catalog() 按开关加载，静态分析可能漏；显式声明兜底
    'vbhelper.features.deps',
    'vbhelper.features.apiwork',
    'vbhelper.features.mcp',
    'vbhelper.features.rag',
    'vbhelper.services.rag.engine',
    'vbhelper.services.web.extract',
    'vbhelper.services.web.search',
    'vbhelper.services.win.clipboard',
    'vbhelper.services.win.input',
    'vbhelper.services.win.screen',
    'vbhelper.viewers.decompiler',
    'vbhelper.viewers.hexview',
    'vbhelper.viewers.jarview',
    'vbhelper.viewers.javaclass',
    'vbhelper.viewers.kbox',
    'vbhelper.viewers.pe',
    'vbhelper.viewers.peview',
    'vbhelper.viewers.textview',
]


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'PIL._avif'],
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
    name='VBHelper',
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
    icon='vbhelper/assets/vbhelper.ico',
)
