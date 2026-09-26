"""Offline speech-to-text support built on Vosk.

The Vosk models themselves are **not** bundled or downloaded on demand. They come
from the signed model catalog (see :mod:`app.core.model_catalog`) and are
installed by the user on the Models screen, so recognition is offered only once
a model for that language is actually on disk.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from app.core.errors import AppError, MissingDependencyError, module_installed
from app.core.model_manager import ModelManager, get_manager

# Vosk names a few languages differently than BCP-47 primary subtags.
NAME_MAP = {"en": "en-us", "zh": "cn"}


def model_base(code: str) -> str:
    primary = (code or "en").split("-")[0].lower() or "en"
    return NAME_MAP.get(primary, primary)


def _installed_dir(model_dir: Path, lang: str) -> Path:
    return Path(model_dir) / f"small_{lang}"


def _contains_model(dir_path: Path) -> bool:
    return (dir_path / "am").is_dir() and (dir_path / "graph").is_dir()


class VoskModelManager:
    """Loads an already-installed Vosk model and recognises speech with it."""

    def __init__(
        self,
        model_dir: Path,
        language: str = "en",
        manager: ModelManager | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.language = language
        self._manager = manager
        self._models: dict[str, object] = {}
        self._recognizer = None
        self._recognizer_lang = ""
        self._model_info: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def manager(self) -> ModelManager:
        if self._manager is None:
            self._manager = get_manager()
        return self._manager

    # ---------------------------------------------------------------- status
    def _installed_dir_for(self, lang: str) -> Path:
        return _installed_dir(self.model_dir, lang)

    def supported_installed(self, code: str = "") -> bool:
        """True when a Vosk model for this language is present on disk."""
        return _contains_model(self._installed_dir_for(model_base(code or self.language)))

    def model_info(self, code: str = "") -> str:
        primary = model_base(code or self.language)
        cached = self._model_info.get(primary)
        if cached:
            return cached
        if _contains_model(self._installed_dir_for(primary)):
            return "model installed"
        return "no offline model — install one from the Models screen"

    def catalog_status(self, code: str = "") -> str:
        """Describe the catalog entry for this language, if there is one."""
        primary = model_base(code or self.language)
        spec = self.manager.find_any(kind="stt", engine="vosk", language=primary)
        if spec is None:
            return "no Vosk model published for this language"
        return self.manager.status_line(spec)

    def spec_for(self, code: str = ""):
        primary = model_base(code or self.language)
        return self.manager.find_any(kind="stt", engine="vosk", language=primary)

    # ----------------------------------------------------------------- ensure
    def ensure(self, language: str = "", on_status: Callable[[str], None] | None = None):
        """Return the loaded Vosk model for ``language``.

        The model must already be installed. If it is not, this raises
        ``ModelNotInstalledError``, which the UI turns into a prompt to open the
        Models screen — it never downloads anything behind the user's back.
        """
        import vosk

        lang = model_base(language or self.language)
        target = self._installed_dir_for(lang)
        if not _contains_model(target):
            self._require_catalog_model(lang)

        with self._lock:
            model = self._models.get(lang)
            if model is None:
                on_status and on_status("Loading offline model…")
                try:
                    model = vosk.Model(str(target))
                except Exception as exc:
                    raise AppError(f"Could not load the Vosk model: {exc}") from exc
                self._models[lang] = model
        return self._models[lang]

    def _require_catalog_model(self, lang: str) -> None:
        spec = self.spec_for(lang)
        if spec is None:
            raise AppError(
                f"No offline (Vosk) model is published for '{lang}'. "
                "Offline recognition needs a Vosk model for that language; "
                "check the Models screen for the available list."
            )
        if not spec.is_installed(self.manager.models_root):
            from app.core.errors import ModelNotInstalledError

            raise ModelNotInstalledError(
                spec.id,
                spec.name,
                f"Vosk models are about {spec.download_bytes() / (1024 ** 2):.0f} MB.",
            )
        # The catalog says installed but the Vosk layout is not there: the tree
        # was damaged or hand-edited. Reject rather than half-load it.
        raise AppError(
            f"The installed '{spec.name}' model is missing its expected data. "
            "Delete it from the Models screen and download it again."
        )

    # ------------------------------------------------------------- recognition
    def recognize(self, audio, language: str = "") -> str:
        """Recognise a captured AudioData segment and return its text."""
        if not module_installed("vosk"):
            raise MissingDependencyError("vosk", "vosk")
        import vosk

        lang = model_base(language or self.language)
        model = self.ensure(language or self.language)
        with self._lock:
            if self._recognizer_lang != lang:
                self._recognizer = vosk.KaldiRecognizer(model, 16000)
                self._recognizer_lang = lang
            self._recognizer.AcceptWaveform(audio.get_raw_data())
            result = json.loads(self._recognizer.FinalResult())
        return (result.get("text") or "").strip()
