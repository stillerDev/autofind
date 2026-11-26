"""Build the optional CPython hot-path extension in place.

Usage (from the project root):

    python native/build_native.py

The MSVC compiler must be visible in PATH (a normal "Developer Command
Prompt for VS" environment is enough). The resulting `afnative*.pyd` is
written to the project root next to run.py; if it is missing, every caller
falls back to the pure-Python implementation and nothing changes.
"""

from pathlib import Path
import sys

from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "native" / "afnative.c"

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "build_ext", "--inplace", "--force", "--build-lib", str(ROOT)]
    setup(
        name="afnative",
        version="1.0",
        description="AutoFind native hot path (read + SHA-256 + decode)",
        ext_modules=[Extension("afnative", [str(SOURCE)])],
        script_args=sys.argv[1:],
    )
