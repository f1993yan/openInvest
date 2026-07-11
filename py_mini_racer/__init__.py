"""Stub py_mini_racer — prevents V8 partition_address_space crash on import.

The real py_mini_racer (mini-racer's V8 DLL) crashes on affected Windows/Python
combinations with: FATAL:partition_address_space.cc: Check failed:
!IsConfigurablePoolInitialized().

akshare eagerly imports py_mini_racer from many submodules (cninfo, sina, ths, etc.),
so even `import akshare` can trigger the crash. This stub provides a no-op
MiniRacer class that lets akshare import succeed.

Functions that actually need JS execution (cninfo decryption, etc.) will raise
StubMiniRacerError at runtime. Production A-share history paths should prefer
direct HTTP/JSON providers (Eastmoney/Tencent) and only reach AkShare/Sina as a
last fallback.
"""
from __future__ import annotations

import sys


class StubMiniRacerError(RuntimeError):
    """Raised when code tries to use MiniRacer (V8) through the stub."""


class MiniRacer:
    """Stub V8 JavaScript evaluator — raises on any actual use."""

    def __init__(self, *args, **kwargs):
        raise StubMiniRacerError(
            "py_mini_racer is stubbed out because the real V8 DLL crashes on this machine. "
            "The calling code path requires JS execution which is unavailable."
        )

    def eval(self, *args, **kwargs):
        raise StubMiniRacerError("MiniRacer.eval() is stubbed out.")

    def execute(self, *args, **kwargs):
        raise StubMiniRacerError("MiniRacer.execute() is stubbed out.")

    def call(self, *args, **kwargs):
        raise StubMiniRacerError("MiniRacer.call() is stubbed out.")


# Compatibility with older releases that use
# ``from py_mini_racer import py_mini_racer`` and then access MiniRacer.
py_mini_racer = sys.modules[__name__]

__all__ = ["MiniRacer", "StubMiniRacerError", "py_mini_racer"]
