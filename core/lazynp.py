"""Lazy numpy proxy.

Importing numpy costs ~70 ms on Windows, which is most of a cold `search`
startup. Most CLI queries (single word, exact/prefix hit) never touch a
vector, so the modules that only need numpy at call time import this proxy
instead and pay the real import on first array access.
"""

from __future__ import annotations


class _LazyNumpy:
    __slots__ = ("_module",)

    def __init__(self) -> None:
        self._module = None

    def _load(self):
        module = self._module
        if module is None:
            import numpy as module
            self._module = module
        return module

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __dir__(self):
        return dir(self._load())


np = _LazyNumpy()
