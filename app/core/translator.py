"""Offline text translation using **Argos Translate**.

Fully local and free: uses ``argostranslate`` with local language packages. A
needed language pair is downloaded once (free) *after the user agrees*, after
which translation works with no internet. No API keys, no cloud accounts.

The public API is unchanged: ``Translator.supported_codes()`` and
``Translator().translate(text, target=...)``.
"""
from __future__ import annotations

import re
import zipfile

# Imported for its side effect: app.config points Argos' XDG paths at the app's
# own models folder. Without it a language pair installed by this module lands
# in ~/.local and the app never sees it. config imports nothing from app.core,
# so this cannot cycle.
import app.config as _config  # noqa: F401
from app.core.errors import (
    AppError,
    DllLoadError,
    MissingDependencyError,
    NetworkError,
    TranslationError,
    TranslationModelConsentRequired,
    is_dll_load_error,
    module_installed,
    soft_import_detail,
)

# Languages Argos Translate actually publishes packages for. This used to be a
# hand-written list of 73 codes, 31 of which had no package behind them -- every
# one of those failed with "No offline translation package exists" the moment a
# user picked it. Keep this in step with the Argos package index; ``check_codes``
# in the tests asserts there are no dead entries.
SUPPORTED_CODES = [
    "ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el",
    "en", "eo", "es", "et", "eu", "fa", "fi", "fr", "ga",
    "gl", "he", "hi", "hu", "id", "it", "ja", "ko", "ky",
    "lt", "lv", "ms", "nb", "nl", "pb", "pl", "pt", "ro",
    "ru", "sk", "sl", "sq", "sv", "sw", "th", "tl", "tr",
    "uk", "ur", "vi", "zh", "zt",
]


def _display_name(code: str) -> str:
    """Human name for a language code."""
    from app.config import language_display_name

    return language_display_name(code) or code


def _guess_source(text: str) -> str:
    """Cheap script-based source guess (Argos needs an explicit source code)."""
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    # Checked before the generic Arabic block because Urdu is written in the
    # Arabic script and shares its whole U+0600-U+06FF range, so the test below
    # used to claim every Urdu sentence as Arabic: the wrong language package
    # was looked for, downloaded with consent, and its output returned with no
    # error at any layer. These six letters are the ones Urdu uses and standard
    # Arabic does not, so their presence is an unambiguous signal.
    if re.search(r"[\u067E\u0686\u0698\u06BA\u06D2\u06D3]", text):
        return "ur"
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    if re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", text):
        return "zh"
    if re.search(r"[\u0E00-\u0E7F]", text):
        return "th"
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u0980-\u09FF]", text):
        return "bn"
    if re.search(r"[\u0B00-\u0B7F]", text):
        return "or"
    if re.search(r"[\u0B80-\u0BFF]", text):
        return "ta"
    if re.search(r"[\u0C00-\u0C7F]", text):
        return "te"
    if re.search(r"[\u0C80-\u0CFF]", text):
        return "kn"
    if re.search(r"[\u0D00-\u0D7F]", text):
        return "ml"
    return "en"


class Translator:
    """Translate text between languages using local Argos models (offline)."""

    def __init__(self, target: str = "en"):
        self.target = target

    @staticmethod
    def available() -> bool:
        return module_installed("argostranslate")

    @staticmethod
    def supported_codes() -> list[str]:
        return list(SUPPORTED_CODES)

    def translate(
        self,
        text: str,
        source: str = "auto",
        target: str = "",
        allow_download: bool = False,
    ) -> str:
        """Translate ``text`` into ``target`` and return the result (offline).

        Nothing is ever downloaded unless ``allow_download`` is True. When the
        language pair is missing this raises
        :class:`~app.core.errors.TranslationModelConsentRequired` so the caller
        can ask the user first, then call again with ``allow_download=True``.
        """
        target = (target or self.target).strip().lower()
        if not target:
            raise TranslationError("Choose a target language to translate into.")
        text = (text or "").strip()
        if not text:
            raise TranslationError("Nothing to translate.")

        if not module_installed("argostranslate"):
            raise MissingDependencyError(
                "argostranslate", "argostranslate",
                detail="Offline translation needs Argos:  pip install argostranslate",
            )

        src = (source or "auto").strip().lower()
        if src in ("", "auto"):
            src = _guess_source(text)
        if src not in SUPPORTED_CODES:
            # A script we recognise but cannot translate offline. Failing here
            # beats silently feeding the text to an English model.
            raise TranslationError(
                f"Offline translation does not support {_display_name(src)}. "
                "Pick a different source language, or translate the text first."
            )

        result, failure = self._try_translate(text, src, target)
        if result:
            return result

        # A failure here means the model *is* installed but would not run. Do not
        # re-download: that was the old behaviour and it reported a bogus
        # "connect to the internet" message for a local DLL conflict.
        if failure is not None:
            raise self._as_app_error(failure, src, target)

        if not allow_download:
            raise TranslationModelConsentRequired(src, target)

        # One-time (free) download of the missing offline language pair.
        self._ensure_package(src, target)
        result, failure = self._try_translate(text, src, target)
        if result:
            return result
        if failure is not None:
            raise self._as_app_error(failure, src, target)
        raise TranslationError(f"No offline translation available for {src} -> {target}.")

    # ------------------------------------------------------------- internals
    @staticmethod
    def _find_language(languages, code: str):
        code = (code or "").lower()
        primary = code.split("-")[0]
        for lang in languages:
            if (lang.code or "").lower() == code:
                return lang
        for lang in languages:
            if (lang.code or "").lower().split("-")[0] == primary:
                return lang
        return None

    def _try_translate(
        self, text: str, src: str, target: str
    ) -> tuple[str, Exception | None]:
        """Attempt the translation.

        Returns ``(text, None)`` on success, ``("", None)`` when the pair is
        simply not installed yet, and ``("", exc)`` when the model exists but
        failed to run -- which the caller must report rather than paper over.
        """
        tr, import_error = soft_import_detail("argostranslate.translate")
        if tr is None:
            return "", import_error
        try:
            languages = tr.get_installed_languages()
        except Exception as exc:
            return "", exc
        src_lang = self._find_language(languages, src)
        tgt_lang = self._find_language(languages, target)
        if not src_lang or not tgt_lang:
            return "", None
        try:
            translation = src_lang.get_translation(tgt_lang)
            if translation:
                return (translation.translate(text) or "").strip(), None
        except Exception as exc:
            return "", exc
        return "", None

    @staticmethod
    def _as_app_error(exc: Exception, src: str, target: str) -> AppError:
        """Turn a raw engine failure into something the user can act on."""
        if is_dll_load_error(exc):
            return DllLoadError("ctranslate2", detail=f"Needed for {src} -> {target}.")
        return TranslationError(
            f"The offline {src} -> {target} model is installed but would not "
            f"run: {exc}"
        )

    @staticmethod
    def _match_package(packages, src: str, target: str):
        """Find the Argos package for a pair, preferring an exact target code.

        ``startswith`` alone would let ``en -> en_us`` satisfy a request for
        ``en -> en``, so exact matches win and prefixes are only a fallback.
        """
        primary = src.split("-")[0]
        loose = None
        for p in packages:
            if (p.from_code or "").split("-")[0] != primary:
                continue
            to_code = (p.to_code or "").lower()
            if to_code == target:
                return p
            if loose is None and to_code.startswith(target):
                loose = p
        return loose

    def _ensure_package(self, src: str, target: str) -> None:
        pkg_mod, import_error = soft_import_detail("argostranslate.package")
        if pkg_mod is None:
            if import_error is not None:
                raise self._as_app_error(import_error, src, target)
            raise MissingDependencyError(
                "argostranslate", "argostranslate",
                detail="Offline translation needs Argos:  pip install argostranslate",
            )
        try:
            pkg_mod.update_package_index()
            packages = pkg_mod.get_available_packages()
        except Exception as exc:
            if is_dll_load_error(exc):
                raise self._as_app_error(exc, src, target) from exc
            raise NetworkError(
                "The offline translation model is not installed and could not be "
                f"fetched (one-time internet needed). ({exc})"
            ) from exc
        match = self._match_package(packages, src, target)
        if match is None:
            raise TranslationError(
                f"No offline translation package exists for {src} -> {target}."
            )
        try:
            archive = match.download()
            if not zipfile.is_zipfile(archive):
                # An interrupted download leaves a truncated file that Argos would
                # happily reuse forever. Drop it and fetch a clean copy.
                archive.unlink(missing_ok=True)
                archive = match.download()
            pkg_mod.install_from_path(archive)
        except Exception as exc:
            if is_dll_load_error(exc):
                raise self._as_app_error(exc, src, target) from exc
            raise NetworkError(
                "Could not install the offline translation model. Connect to "
                f"the internet once, then try again. ({exc})"
            ) from exc
