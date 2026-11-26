# -*- mode: python ; coding: utf-8 -*-
# Shared diagnostic-build definition. afdiag2.spec execs this file in its own
# namespace; PyInstaller's `specnm` supplies the output name. One definition,
# two build names.
import os

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

# The CRT DLLs belong to Windows, not to the Python install. Resolve them from
# the real system root: a hard-coded C:/Windows silently breaks on a redirected
# or non-default Windows directory.
SYSTEM32 = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")
CRT_DLLS = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcruntime140_threads.dll",
)
binaries = [(os.path.join(SYSTEM32, name), ".") for name in CRT_DLLS]

datas = []
hiddenimports = ['core.ocr.engine']
datas += collect_data_files('rapidocr_onnxruntime')
hiddenimports += collect_submodules('winrt')


a = Analysis(
    ['_diag.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name=specnm,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    upx=True,
    upx_exclude=[],
    name=specnm,
)
