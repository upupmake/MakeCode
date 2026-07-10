# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import glob

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

project_root = os.path.abspath(SPECPATH)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Keep every runtime module owned by this project, including modules imported lazily.
project_hiddenimports = [
    'init',
    'prompts',
    'settings',
    'version',
]
for package in ('system', 'tools', 'utils'):
    project_hiddenimports.extend(collect_submodules(package))

# --- Custom logic to collect ts_cache files ---
ts_cache_datas = []
ts_cache_dir = os.path.join(project_root, 'ts_cache')
if os.path.exists(ts_cache_dir):
    # Only collect .tar.zst files
    for file in glob.glob(os.path.join(ts_cache_dir, '*.tar.zst')):
        ts_cache_datas.append((file, 'ts_cache'))

    manifest_path = os.path.join(ts_cache_dir, 'manifest.json')
    if os.path.exists(manifest_path):
        ts_cache_datas.append((manifest_path, 'ts_cache'))
# ----------------------------------------------

updater_path = os.path.join(project_root, 'dist', 'updater.exe')
if not os.path.isfile(updater_path):
    raise FileNotFoundError(
        'dist/updater.exe does not exist; run pyinstaller updater.spec first'
    )

icon_path = os.path.join(project_root, 'assets', 'logo.ico')
if not os.path.isfile(icon_path):
    raise FileNotFoundError('assets/logo.ico does not exist')

a = Analysis(
    [os.path.join(project_root, 'main.py')],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, 'tiktoken_cache'), 'tiktoken_cache'),
        (updater_path, '.'),
    ] + ts_cache_datas + copy_metadata('fastmcp'),
    hiddenimports=project_hiddenimports + [
        'tiktoken_ext.openai_public',
        'tiktoken_ext',
        'rich',
        'textual',
        'pyzstd',
        'tree_sitter',
        'tree_sitter_language_pack'
    ],
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
    icon=[icon_path],
)
