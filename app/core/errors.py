"""Friendly error types and soft dependency guards.

Every optional third-party library in the app is loaded through the helpers in
this module so that a missing library never crashes the application with an
obscure traceback — instead the user gets an actionable message with the exact
``pip install`` command.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import threading

# The optional AI packages (torch, ctranslate2, sentencepiece, TTS, ...) each
# load their own native DLLs. Windows aborts the process with an access
# violation when two of them are loaded concurrently from different threads, so
# every soft import is serialized behind this lock.
_IMPORT_LOCK = threading.RLock()


class AppError(Exception):
    """Base class for all application-level errors."""


class MissingDependencyError(AppError):
    """Raised when an optional third-party library is required but absent."""

    def __init__(self, module: str, pip_hint: str, detail: str = ""):
        self.module = module
        self.pip_hint = pip_hint
        self.detail = detail
        msg = (
            f"Missing required library '{module}'.\n"
            f"Install it with:  pip install {pip_hint}"
        )
        if detail:
            msg += f"\n{detail}"
        super().__init__(msg)


class NetworkError(AppError):
    """Raised when an operation needs internet access but the request fails."""


class DllLoadError(AppError):
    """Raised when a native library is present but fails to initialise.

    Windows reports this as ``[WinError 1114]`` ("DLL initialization routine
    failed"). The usual cause on this app's Windows build is native runtime
    *load order*: Qt5, CTranslate2 and torch each ship their own C++/OpenMP
    runtime, Windows binds a DLL by base name, so the first one loaded is the
    one all the others inherit. If Qt is imported before CTranslate2, the first
    translation dies here even though every package is correctly installed --
    so re-installing or reconnecting to the internet cannot help.

    ``main.py`` calls
    :func:`app.core.native_runtime.preload_native_runtime` before importing
    PyQt5 precisely to make that order deterministic. Reaching this error means
    that guard did not hold, so the report says so instead of advising a
    restart the user has already tried.
    """

    def __init__(self, module: str, detail: str = ""):
        self.module = module
        self.detail = detail
        msg = (
            f"The '{module}' native library could not start "
            f"(Windows DLL error 1114).{(' ' + detail) if detail else ''}"
            "\n\nThis is a native library load-order conflict between the Qt UI "
            "and the AI runtimes, not a missing or partial install, so "
            "re-installing packages will not change it."
            "\n\nWhat to do:"
            "\n  1. Fully quit the app (check the tray and Task Manager) and start it"
            "\n     again -- a second copy of the app running at once is the usual"
            "\n     cause."
            "\n  2. If it still fails, report this with the text above. It points at "
            f"the\n     'app.core.native_runtime' preload in main.py, not at {module}."
        )
        super().__init__(msg)


class DeviceError(AppError):
    """Raised when an audio device (mic/speaker) is unavailable or fails."""


class MicPermissionError(DeviceError):
    """Raised when the microphone cannot be opened (privacy/disconnected)."""


class TTSGenerationError(AppError):
    """Raised when speech synthesis fails for a non-library reason."""


class TranslationError(AppError):
    """Raised when text translation between languages fails."""


class TranslationModelConsentRequired(AppError):
    """Raised when a language pair must be downloaded before it can be used.

    Carries the resolved language pair so the UI can name it in the prompt.
    Nothing has been downloaded when this is raised.
    """

    def __init__(self, source: str, target: str, size_hint: str = ""):
        self.source = source
        self.target = target
        self.size_hint = size_hint
        msg = (
            f"The offline {source} -> {target} model is not installed yet. "
            "It downloads once, then works with no internet."
        )
        if size_hint:
            msg += f" ({size_hint})"
        super().__init__(msg)


class CloneModelError(AppError):
    """Raised when the voice-cloning model cannot load or synthesize."""


class ModelError(AppError):
    """Raised when a model catalog entry or download is invalid."""


class ModelNotInstalledError(ModelError):
    """Raised when an engine is selected but its model is not on disk."""

    def __init__(self, spec_id: str, display_name: str, detail: str = ""):
        self.spec_id = spec_id
        self.display_name = display_name
        msg = (
            f"'{display_name}' is not installed yet.\n"
            "Open the Models screen to download it before using this engine."
        )
        if detail:
            msg += f"\n{detail}"
        super().__init__(msg)


def module_available(module_name: str) -> bool:
    """Return True if the given Python module can be imported."""
    with _IMPORT_LOCK:
        try:
            importlib.import_module(module_name)
            return True
        except Exception:
            return False


def module_installed(module_name: str) -> bool:
    """Return True if the module is installed, without importing it.

    Like ``module_available`` but does not execute the module's top-level
    code, so heavy imports (torch, qwen_tts, TTS, ...) stay lazy at startup.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def soft_import(module_name: str) -> object | None:
    """Import a module and return it, or None if unavailable.

    Already-imported modules are returned straight from ``sys.modules`` so the
    lock is only paid for on a real first import.
    """
    module, _ = soft_import_detail(module_name)
    return module


def soft_import_detail(module_name: str) -> tuple[object | None, Exception | None]:
    """Like :func:`soft_import` but also return *why* the import failed.

    ``soft_import`` throws the reason away, which makes a native DLL failure
    (WinError 1114) indistinguishable from a missing package. Callers that need
    to tell those apart -- and to report something the user can act on -- use
    this instead.
    """
    module = sys.modules.get(module_name)
    if module is not None:
        return module, None
    with _IMPORT_LOCK:
        try:
            return importlib.import_module(module_name), None
        except Exception as exc:
            return None, exc


#: ``winerror`` values that mean "the library is installed but its native code
#: would not start" rather than "the package is absent".
_DLL_INIT_WINERRORS = frozenset({5, 126, 1114})


def is_dll_load_error(exc: BaseException | None) -> bool:
    """True when ``exc`` is a native-library initialisation failure."""
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "winerror", None) in _DLL_INIT_WINERRORS


def require(module_name: str, pip_hint: str) -> object:
    """Import a module or raise a friendly MissingDependencyError."""
    mod = soft_import(module_name)
    if mod is None:
        raise MissingDependencyError(module_name, pip_hint)
    return mod
