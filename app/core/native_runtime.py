"""Native runtime load-order guard for the desktop app.

Windows resolves an imported DLL **by base name**, so the first runtime of a
given name bound into the process wins, and every later consumer binds to that
one whether it was linked against it or not. This matters on Windows only,
which is also the only platform this app ships on.

Qt5 and CTranslate2 ship *different builds* of the C++ and OpenMP runtimes:

    PyQt5\\Qt5\\bin\\msvcp140.dll        590,112 bytes
    sklearn\\.libs\\msvcp140.dll       643,512 bytes
    ctranslate2\\libiomp5md.dll   build 20250910
    torch\\lib\\libiomp5md.dll     build 20260213

``ctranslate2.dll`` (Argos Translate) and ``torch_cpu.dll`` (Whisper) each
import ``libiomp5md.dll`` dynamically. If Qt is imported first -- which
``main.py`` used to do at module scope -- Qt's copy of the C++ runtime is bound
before CTranslate2 ever initialises, and the first translation then dies with
``[WinError 1114] DLL initialization routine failed``. That surfaced to the user
as ``DllLoadError`` on *both* the Text to Speech and Speech to Text screens,
because both call the same translator.

The fix is to make the order deterministic instead of accidental: import
CTranslate2 first, and actually run one cheap native call so its runtime is
initialised before anything else can claim it. Doing the cheap call matters --
importing the extension alone was not enough, the failure showed up on first
use in some orderings.

Importing CTranslate2 directly (rather than preloading torch) keeps this
honest: CTranslate2 ships in the core offline requirements, while torch belongs
to the optional voice-cloning stack and costs several seconds to import.

The functions here never raise. Translation already reports a missing or
broken dependency precisely, with :class:`~app.core.errors.DllLoadError`, and
this must not pre-empt that with an import-time crash.
"""
from __future__ import annotations

import sys
import threading

from app.core.errors import is_dll_load_error, soft_import_detail

_lock = threading.Lock()
_loaded = False

#: Module imported to establish the native runtime baseline.
NATIVE_MODULE = "ctranslate2"


def _warm_native_runtime(module: object) -> bool:
    """Run one cheap native call so *module*'s runtime really initialises.

    ``get_supported_compute_types`` queries the library for what the CPU backend
    can do. It needs no model and no network, so it is safe to run at startup,
    and it touches the parts of CTranslate2 that the later translation depends
    on.
    """
    query = getattr(module, "get_supported_compute_types", None)
    if not callable(query):
        return False
    try:
        query("cpu", 1)
    except Exception:
        return False
    return True


def preload_native_runtime() -> bool:
    """Bind the C++/OpenMP runtime that the Qt UI and Argos can both live with.

    Returns ``True`` when a native runtime was initialised first, ``False`` when
    the module is unavailable or unusable. Safe and cheap to call repeatedly.
    """
    global _loaded
    if _loaded:
        return True
    with _lock:
        if _loaded:
            return True
        module, exc = soft_import_detail(NATIVE_MODULE)
        if module is None:
            # A missing package, or a genuinely broken install, is the
            # translator's error to report with proper context. Swallowing it
            # here keeps startup alive and leaves that message intact.
            if exc is not None and is_dll_load_error(exc):
                sys.stderr.write(
                    f"[native-runtime] {NATIVE_MODULE} could not initialise "
                    f"({exc}); Qt may conflict with it at translation time.\n"
                )
            return False
        if not _warm_native_runtime(module):
            return False
        _loaded = True
        return True


def native_runtime_ready() -> bool:
    """Whether :func:`preload_native_runtime` has already succeeded."""
    return _loaded
