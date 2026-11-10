# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('web', 'web'), ('models/manifest.json', 'models')]
binaries = []
hiddenimports = ['clr', 'pypdf', 'docx', 'watchdog.observers']
# rapidocr 3.x: config yaml + the bundled `multi` recognition model. The
# per-script models stay on demand (~8 MB each, downloaded on first use).
datas += collect_data_files('rapidocr')
hiddenimports += collect_submodules('winrt')
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # torch is installed in this environment but nothing in the app imports
    # it; without the exclusion a community hook drags 3 GB of it into the
    # exe. pandas/pyarrow/scipy/numba come in through pymupdf's optional
    # layout-analysis layer, which `page.get_text` never touches. rapidocr
    # does need shapely and pyclipper — those stay.
    excludes=[
        'tkinter', 'matplotlib', 'PyQt5', 'PySide6',
        'torch', 'torchvision', 'torchaudio', 'tensorboard',
        'pandas', 'pyarrow', 'scipy', 'numba', 'llvmlite',
        'pymupdf4llm', 'pymupdf.layout',
        'pydoc_data', 'unittest', 'xmlrpc',
    ],
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
    name='AutoFind',
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
    icon=['web/icon.ico'],
)
