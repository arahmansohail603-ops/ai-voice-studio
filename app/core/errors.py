"""Friendly error types and soft dependency guards.

Every optional third-party library in the app is loaded through the helpers in
this module so that a missing library never crashes the application with an
obscure traceback — instead the user gets an actionable message with the exact
``pip install`` command.
"""
from __future__ import annotations

import importlib
from typing import Optional


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


class DeviceError(AppError):
    """Raised when an audio device (mic/speaker) is unavailable or fails."""


class MicPermissionError(DeviceError):
    """Raised when the microphone cannot be opened (privacy/disconnected)."""


class TTSGenerationError(AppError):
    """Raised when speech synthesis fails for a non-library reason."""


class CloneModelError(AppError):
    """Raised when the voice-cloning model cannot load or synthesize."""


def module_available(module_name: str) -> bool:
    """Return True if the given Python module can be imported."""
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def soft_import(module_name: str) -> Optional[object]:
    """Import a module and return it, or None if unavailable."""
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def require(module_name: str, pip_hint: str) -> object:
    """Import a module or raise a friendly MissingDependencyError."""
    mod = soft_import(module_name)
    if mod is None:
        raise MissingDependencyError(module_name, pip_hint)
    return mod
