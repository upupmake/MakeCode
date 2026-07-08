# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import glob

project_root = os.path.abspath(SPECPATH)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 1. 在文件开头引入 copy_metadata 模块
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# --- Custom logic to collect ts_cache files ---
ts_cache_datas = []
ts_cache_dir = 'ts_cache'
if os.path.exists(ts_cache_dir):
    # Only collect .tar.zst files
    for file in glob.glob(os.path.join(ts_cache_dir, '*.tar.zst')):
        ts_cache_datas.append((file, 'ts_cache'))
    
    manifest_path = os.path.join(ts_cache_dir, 'manifest.json')
    if os.path.exists(manifest_path):
         ts_cache_datas.append((manifest_path, 'ts_cache'))
# ----------------------------------------------

a = Analysis(
    ['main.py'],
    pathex=[project_root],
    binaries=[],
    # 2. 将原来的 datas 列表与 copy_metadata 的结果相加
    datas=[('tiktoken_cache', 'tiktoken_cache')] + ts_cache_datas + copy_metadata('fastmcp') + [('dist/updater.exe', '.')],
    hiddenimports=[
        'utils.common',
        'tiktoken_ext.openai_public',
        'tiktoken_ext',
        'rich',
        'textual',
        'pyzstd',
        'tree_sitter',
        'tree_sitter_language_pack'
    ] + collect_submodules('system') + collect_submodules('tools') + collect_submodules('utils'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'matplotlib', 'PIL', 'pandas', 'openpyxl', 'xlrd', 'pytest'],
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
    name='MakeCode',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\logo.ico'],
)
