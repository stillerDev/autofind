# -*- mode: python ; coding: utf-8 -*-
# Parameterised twin of afdiag.spec. The build definition lives in one file;
# PyInstaller's `specnm` is "afdiag2" in this namespace, so the shared spec
# produces a second build under this name without a copied 50-line spec.
from pathlib import Path

exec(
    compile(
        (Path(SPECPATH) / "afdiag.spec").read_text(encoding="utf-8"),
        "afdiag.spec",
        "exec",
    ),
    globals(),
)
