# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

构建：`build.bat`（Windows）或 `python -m PyInstaller VBHelper.spec`（其它平台）。

不要用命令行参数重新生成 spec —— `--specpath` 会把本文件覆盖成默认模板，
hiddenimports 和资源清单会一起丢失。

跨平台说明：PyInstaller 不做交叉编译，Linux 产物必须在 Linux 上打；
CI 见 .github/workflows/build.yml。
"""
import sys

from PyInstaller.utils.hooks import collect_data_files

# Windows 用 ico，其它平台 PyInstaller 只接受 png
ICON = ('vbhelper/assets/vbhelper.ico' if sys.platform == 'win32'
        else 'vbhelper/assets/vbhelper_256.png')

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
    icon=ICON,
)
