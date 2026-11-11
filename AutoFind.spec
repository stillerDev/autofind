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
        # rapidocr's optional TensorRT backend. It pulls nvinfer_10.dll and
        # friends from the CUDA toolkit when that is installed — 440 MB of
        # binaries for an engine the app never selects (it uses onnxruntime).
        # On a machine without CUDA the same wheels are simply absent.
        'tensorrt', 'tensorrt_libs', 'tensorrt_bindings',
        'rapidocr.inference_engine.tensorrt',
    ],
    noarchive=False,
    optimize=0,
)
a.binaries = [
    entry for entry in a.binaries
    if not any(marker in entry[0].lower()
               for marker in ('nvinfer', 'nvonnxparser', 'tensorrt'))
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoFind',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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

# One-folder, not one-file. A one-file build unpacks the whole runtime into a
# temp directory on every launch; at this size (OCR models, OpenCV, MuPDF) that
# was 6 s of disk churn before the first window. The folder starts in about the
# time plain Python does because nothing is extracted.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoFind',
)
