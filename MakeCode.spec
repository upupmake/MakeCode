# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import platform

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
language_pack_hiddenimports = collect_submodules('tree_sitter_language_pack')

# --- Custom logic to collect the current platform's ts_cache file ---
ts_cache_datas = []
ts_cache_dir = os.path.join(project_root, 'ts_cache')
machine = platform.machine().lower()
arch = 'x86_64' if machine in {'amd64', 'x86_64'} else 'aarch64' if machine in {'arm64', 'aarch64'} else None
system_name = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else 'macos' if sys.platform == 'darwin' else None
if system_name and arch:
    platform_key = 'macos-arm64' if system_name == 'macos' and arch == 'aarch64' else f'{system_name}-{arch}'
    parser_archive = os.path.join(ts_cache_dir, f'parsers-{platform_key}.tar.zst')
    if os.path.isfile(parser_archive):
        ts_cache_datas.append((parser_archive, 'ts_cache'))
    else:
        print(f'WARNING: tree-sitter parser archive not found for {platform_key}; syntax validation will fail open')

manifest_path = os.path.join(ts_cache_dir, 'manifest.json')
if os.path.isfile(manifest_path):
    ts_cache_datas.append((manifest_path, 'ts_cache'))
# -------------------------------------------------------------------

updater_datas = []
if sys.platform == 'win32':
    updater_path = os.path.join(project_root, 'dist', 'updater.exe')
    if not os.path.isfile(updater_path):
        raise FileNotFoundError(
            'dist/updater.exe does not exist; run pyinstaller updater.spec first'
        )
    updater_datas.append((updater_path, '.'))

icon_path = os.path.join(project_root, 'assets', 'logo.ico')
if not os.path.isfile(icon_path):
    raise FileNotFoundError('assets/logo.ico does not exist')

a = Analysis(
    [os.path.join(project_root, 'main.py')],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, 'tiktoken_cache'), 'tiktoken_cache'),
    ] + updater_datas + ts_cache_datas + copy_metadata('fastmcp') + copy_metadata('tree-sitter-language-pack'),
    hiddenimports=project_hiddenimports + language_pack_hiddenimports + [
        'tiktoken_ext.openai_public',
        'tiktoken_ext',
        'rich',
        'textual',
        'pyzstd',
        'tree_sitter'
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
    [],
    exclude_binaries=True,
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
    icon=[icon_path] if sys.platform == 'win32' else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MakeCode',
)
