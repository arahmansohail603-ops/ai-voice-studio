"""The Piper voice library: browse and download offline neural voices.

Piper publishes ~180 voice models covering ~55 languages. Until now the app had
no way to fetch one: :mod:`app.core.local_models` only *discovers* ``.onnx``
files a user had already placed in ``models/piper-voices/`` by hand, and the
signed catalog ships empty. That is why "translate to Urdu" failed on a machine
whose only voices were English -- the translation succeeded, then synthesis had
nothing to speak with.

The catalog is ``voices.json`` from the public ``rhasspy/piper-voices`` dataset.
It is cached in the models folder on first fetch, so the Voices screen works
offline afterwards. Downloads verify the published size and MD5 digest and land
via a temporary file plus an atomic rename, so an interrupted download can
never leave a half-written ``.onnx`` that Piper would fail to load later.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.config import PIPER_VOICE_DIR
from app.core.errors import AppError, NetworkError, TTSGenerationError

#: The voice catalog. ``resolve/main`` is what the Piper tooling itself uses.
VOICES_INDEX_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
)
_FILE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

#: What the app can do about a language's voice. ``UNKNOWN`` is load-bearing: the
#: catalog is only ever read from disk, so a genuine first run has nothing
#: cached and must not be reported as "no voice exists".
INSTALLED = "installed"
DOWNLOADABLE = "downloadable"
UNPUBLISHED = "unpublished"
UNKNOWN = "unknown"

#: Quality tiers, worst to best. ``x_low`` is small and fast, ``high`` is large
#: and noticeably slower to synthesise.
QUALITY_ORDER = ("x_low", "low", "medium", "high")
_QUALITY_LABELS = {
    "x_low": "Fastest (smallest)",
    "low": "Fast",
    "medium": "Balanced",
    "high": "Best (largest)",
}

_TIMEOUT = 60
_CHUNK = 256 * 1024
#: A voice is an ONNX graph plus its JSON config. Anything wildly larger than the
#: largest published voice is a bad server or a redirect to a web page.
_MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_INDEX_BYTES = 8 * 1024 * 1024

STAGE_DOWNLOADING = "downloading"
STAGE_VERIFYING = "verifying"
STAGE_INSTALLED = "installed"

ProgressCallback = Callable[[str, int, int, str], None]


class VoiceEntry:
    """One downloadable Piper voice."""

    __slots__ = (
        "key",
        "name",
        "language_code",
        "language_family",
        "native_name",
        "english_name",
        "country",
        "quality",
        "speakers",
        "files",
    )

    def __init__(self, raw: dict) -> None:
        language = raw.get("language") or {}
        self.key = str(raw.get("key") or "")
        self.name = str(raw.get("name") or "")
        self.language_code = str(language.get("code") or "")
        self.language_family = str(language.get("family") or "").lower()
        self.native_name = str(language.get("name_native") or "")
        self.english_name = str(language.get("name_english") or "")
        self.country = str(language.get("country_english") or "")
        self.quality = str(raw.get("quality") or "")
        try:
            self.speakers = int(raw.get("num_speakers") or 1)
        except (TypeError, ValueError):
            self.speakers = 1
        self.files: dict = dict(raw.get("files") or {})

    # ------------------------------------------------------------- helpers
    def model_file(self) -> str | None:
        """Catalog path of the ``.onnx`` file, which is what we install."""
        for path in self.files:
            if path.endswith(".onnx"):
                return path
        return None

    def config_file(self) -> str | None:
        for path in self.files:
            if path.endswith(".onnx.json"):
                return path
        return None

    def model_bytes(self) -> int:
        return self._size(self.model_file())

    def total_bytes(self) -> int:
        """Size of everything a download fetches."""
        return sum(self._size(p) for p in self.files if p.endswith((".onnx", ".onnx.json")))

    def _size(self, path: str | None) -> int:
        if not path:
            return 0
        entry = self.files.get(path) or {}
        if isinstance(entry, dict):
            try:
                return int(entry.get("size_bytes") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def quality_label(self) -> str:
        return _QUALITY_LABELS.get(self.quality, self.quality or "unknown")

    def display(self) -> str:
        """Label for the UI, e.g. ``fasih · Balanced · 61 MB``."""
        size = self.model_bytes()
        megabytes = f"{size / (1024 ** 2):.0f} MB" if size else "size unknown"
        parts = [self.name.replace("_", " ").title(), self.quality_label(), megabytes]
        if self.speakers > 1:
            parts.append(f"{self.speakers} speakers")
        return "  ·  ".join(p for p in parts if p)

    def installed_name(self) -> str:
        """The ``.onnx`` stem Piper uses to look this voice up on disk."""
        model = self.model_file()
        return Path(model).stem if model else self.key


class PiperVoiceLibrary:
    """Browse and install the published Piper voices."""

    def __init__(self, voice_dir: Path | None = None) -> None:
        self.voice_dir = Path(voice_dir or PIPER_VOICE_DIR)

    # -------------------------------------------------------------- catalog
    @property
    def index_path(self) -> Path:
        return self.voice_dir / "voices.json"

    def installed(self) -> set[str]:
        """Stems of the voices already on disk and complete."""
        if not self.voice_dir.is_dir():
            return set()
        found = set()
        try:
            candidates = self.voice_dir.glob("*.onnx")
        except OSError:
            return set()
        for model in candidates:
            if model.with_suffix(".onnx.json").is_file():
                found.add(model.stem)
        return found

    def is_installed(self, entry: VoiceEntry) -> bool:
        return entry.installed_name() in self.installed()

    def _read_cached_index(self) -> dict | None:
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _write_index(self, raw: dict) -> None:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        temp = self.index_path.with_name(self.index_path.name + ".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        os.replace(temp, self.index_path)

    def _fetch_index(self) -> dict:
        request = urllib.request.Request(
            VOICES_INDEX_URL,
            headers={"Accept": "application/json", "User-Agent": self._user_agent()},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(_MAX_INDEX_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise NetworkError(
                f"Could not reach the Piper voice catalog: {exc}"
            ) from exc
        if len(raw) > _MAX_INDEX_BYTES:
            raise NetworkError("The Piper voice catalog is unexpectedly large.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError("The Piper voice catalog is not valid JSON.") from exc

    def catalog(self, refresh: bool = False) -> list[VoiceEntry]:
        """Every published voice.

        Falls back to the cached copy when the network is unavailable, so the
        screen is still usable offline.
        """
        raw = None
        if not refresh:
            raw = self._read_cached_index()
        if raw is None:
            raw = self._fetch_index()
            try:
                self._write_index(raw)
            except OSError:
                # A read-only models folder still leaves us a usable catalog.
                pass
        entries = [VoiceEntry(item) for item in (raw or {}).values()]
        return [e for e in entries if e.model_file() and e.language_family]

    def catalog_from_cache(self) -> list[VoiceEntry]:
        """Cached voices only -- never touches the network."""
        raw = self._read_cached_index() or {}
        entries = [VoiceEntry(item) for item in raw.values()]
        return [e for e in entries if e.model_file() and e.language_family]

    def voices_for_language(self, code: str, entries: list[VoiceEntry] | None = None) -> list[VoiceEntry]:
        """Voices for a language, accepting ``ur``, ``ur_PK`` or ``ur-PK``."""
        wanted = self._normalise(code)
        pool = self.catalog_from_cache() if entries is None else entries
        return [e for e in pool if self._normalise(e.language_family) == wanted
                or self._normalise(e.language_code) == wanted]

    def published_languages(self) -> set[str]:
        """Language families Piper publishes at least one voice for.

        This is deliberately different from "installed". Punjabi, for example,
        is translated into but has no published voice, so no amount of
        downloading can make it speakable -- a fact the UI has to be honest
        about instead of offering a download that cannot succeed.
        """
        return {e.language_family for e in self.catalog_from_cache()}

    def has_published_voice(self, code: str) -> bool:
        """True when a voice for this language exists upstream at all."""
        return self._normalise(code) in self.published_languages()

    def voice_availability(self, code: str) -> str:
        """Classify what can be done about this language's voice.

        Returns one of :data:`INSTALLED`, :data:`DOWNLOADABLE`,
        :data:`UNPUBLISHED` or :data:`UNKNOWN`. The last one is why this is not
        just a bool: the catalog is cache-only, so on a first run the honest
        answer is "I don't know yet", and claiming a language is unspeakable
        would hide a download button that would in fact have worked.
        """
        if self.has_installed_voice(code):
            return INSTALLED
        published = self.published_languages()
        if not published:
            return UNKNOWN
        if self._normalise(code) in published:
            return DOWNLOADABLE
        return UNPUBLISHED

    def can_never_speak(self, code: str) -> bool:
        """True only when the catalog positively proves no voice exists.

        A language Piper has never published cannot be made speakable by
        downloading anything, so the UI must not offer a download for it.
        """
        return self.voice_availability(code) == UNPUBLISHED

    def has_installed_voice(self, code: str) -> bool:
        """True when a voice for this language is already on disk."""
        installed = self.installed()
        return any(
            entry.installed_name() in installed
            for entry in self.voices_for_language(code)
        )

    @staticmethod
    def _normalise(code: str) -> str:
        return (code or "").replace("-", "_").split("_")[0].strip().lower()

    def languages(self, entries: list[VoiceEntry] | None = None) -> list[dict]:
        """One row per language, for the language list.

        ``translatable`` marks the languages Argos can also *translate into*, so
        the UI can be honest about which ones work end-to-end.
        """
        pool = self.catalog_from_cache() if entries is None else entries
        translatable = self._translatable_codes()
        grouped: dict[str, list[VoiceEntry]] = {}
        for entry in pool:
            grouped.setdefault(entry.language_family, []).append(entry)
        rows = []
        for family, voices in grouped.items():
            voices.sort(key=lambda v: (QUALITY_ORDER.index(v.quality)
                                      if v.quality in QUALITY_ORDER else 99, v.name))
            native = next((v.native_name for v in voices if v.native_name), "")
            english = next((v.english_name for v in voices if v.english_name), "")
            installed = [v for v in voices if self.is_installed(v)]
            rows.append(
                {
                    "family": family,
                    "label": english or family.upper(),
                    "native": native,
                    "code": voices[0].language_code,
                    "voices": voices,
                    "count": len(voices),
                    "installed": len(installed),
                    "translatable": family in translatable,
                }
            )
        rows.sort(key=lambda row: (not row["installed"], row["label"].lower()))
        return rows

    @staticmethod
    def _translatable_codes() -> set[str]:
        try:
            from app.core.translator import SUPPORTED_CODES

            return set(SUPPORTED_CODES)
        except Exception:  # pragma: no cover - defensive
            return set()

    # ------------------------------------------------------------ downloads
    def install(
        self,
        entry: VoiceEntry,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """Download and verify a voice. Returns the installed ``.onnx`` path.

        Already-complete voices return immediately, so a retried download costs
        nothing.
        """
        if self.is_installed(entry):
            return self.voice_dir / f"{entry.installed_name()}.onnx"

        wanted = [(p, p.endswith(".onnx")) for p in entry.files
                  if p.endswith((".onnx", ".onnx.json"))]
        if not wanted:
            raise TTSGenerationError(
                f"The catalog entry for '{entry.key}' lists no downloadable files."
            )
        total = sum(entry._size(p) for p, _ in wanted)
        self.voice_dir.mkdir(parents=True, exist_ok=True)

        received = 0
        for remote, is_model in sorted(wanted, key=lambda item: not item[1]):
            self._fetch_file(
                remote,
                self.voice_dir / Path(remote).name,
                entry._size(remote),
                on_progress,
                received=received,
                total=total,
            )
            received += entry._size(remote)
        if on_progress:
            on_progress(STAGE_INSTALLED, total, total, entry.installed_name())
        return self.voice_dir / f"{entry.installed_name()}.onnx"

    def _fetch_file(
        self,
        remote: str,
        destination: Path,
        expected_bytes: int,
        on_progress: ProgressCallback | None,
        received: int = 0,
        total: int = 0,
    ) -> None:
        """Stream one file, verify it, then move it into place atomically."""
        if expected_bytes > _MAX_FILE_BYTES:
            raise TTSGenerationError(
                f"'{destination.name}' is implausibly large "
                f"({expected_bytes} bytes); refusing to download it."
            )
        url = _FILE_BASE + remote
        request = urllib.request.Request(url, headers={"User-Agent": self._user_agent()})
        temp = destination.with_name(destination.name + ".part")
        got = 0
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                with open(temp, "wb") as sink:
                    while True:
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > _MAX_FILE_BYTES:
                            raise TTSGenerationError(
                                f"'{destination.name}' downloaded far larger than "
                                "expected; the download was stopped."
                            )
                        sink.write(chunk)
                        if on_progress:
                            on_progress(
                                STAGE_DOWNLOADING,
                                received + got,
                                total or expected_bytes,
                                destination.name,
                            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            temp.unlink(missing_ok=True)
            raise NetworkError(
                f"Could not download '{destination.name}': {exc}"
            ) from exc
        except Exception:
            temp.unlink(missing_ok=True)
            raise

        if expected_bytes and got != expected_bytes:
            temp.unlink(missing_ok=True)
            raise TTSGenerationError(
                f"'{destination.name}' was {got} bytes but the catalog says "
                f"{expected_bytes}. The download was incomplete."
            )
        if on_progress:
            on_progress(STAGE_VERIFYING, received + got, total or got, destination.name)
        self._verify(temp, destination)
        os.replace(temp, destination)

    def _verify(self, temp: Path, destination: Path) -> None:
        """Confirm the bytes are what the catalog promised, then publish."""
        expected = self._expected_md5(destination.name)
        if expected:
            if self._md5(temp) != expected:
                temp.unlink(missing_ok=True)
                raise TTSGenerationError(
                    f"'{destination.name}' failed its integrity check and was "
                    "discarded. Please try the download again."
                )
        elif temp.stat().st_size == 0:
            temp.unlink(missing_ok=True)
            raise TTSGenerationError(f"'{destination.name}' downloaded empty.")

    def _expected_md5(self, filename: str) -> str | None:
        raw = self._read_cached_index() or {}
        for item in raw.values():
            for remote, meta in (item.get("files") or {}).items():
                if Path(remote).name == filename and isinstance(meta, dict):
                    digest = meta.get("md5_digest")
                    return str(digest) if digest else None
        return None

    @staticmethod
    def _md5(path: Path) -> str:
        digest = hashlib.md5()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def remove(self, entry: VoiceEntry) -> None:
        """Delete an installed voice and its config."""
        if not self.is_installed(entry):
            return
        stem = entry.installed_name()
        for suffix in (".onnx", ".onnx.json"):
            try:
                (self.voice_dir / f"{stem}{suffix}").unlink(missing_ok=True)
            except OSError as exc:
                raise TTSGenerationError(
                    f"Could not delete '{stem}{suffix}': {exc}"
                ) from exc

    def clear_catalog_cache(self) -> None:
        try:
            self.index_path.unlink(missing_ok=True)
        except OSError:
            pass

    def disk_usage(self) -> int:
        total = 0
        if not self.voice_dir.is_dir():
            return 0
        try:
            for entry in self.voice_dir.iterdir():
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            return 0
        return total

    @staticmethod
    def _user_agent() -> str:
        from app.config import APP_NAME, APP_VERSION

        return f"{APP_NAME.replace(' ', '')}/{APP_VERSION}"


def voice_choices() -> list[str]:
    """Convenience for the TTS screen: language codes that have a voice."""
    library = PiperVoiceLibrary()
    return sorted({row["family"] for row in library.languages()})


def published_voice_languages() -> frozenset[str]:
    """Language families with at least one published Piper voice.

    Reads only the cached catalog and never touches the network, so it is safe
    to call while the GUI is still importing. An **empty** result means the
    catalog has not been fetched yet -- callers must read that as "unknown",
    never as "these languages have no voices", or a first run would mark all
    fifty languages as text-only.
    """
    try:
        return frozenset(PiperVoiceLibrary().published_languages())
    except OSError:
        return frozenset()


def is_speakable(code: str) -> bool:
    """True when this language can be spoken once its voice is downloaded.

    Only ever answers ``False`` on a positive result, so a missing catalog
    leaves the UI permissive instead of blocking the user.
    """
    published = published_voice_languages()
    if not published:
        return True
    return PiperVoiceLibrary()._normalise(code) in published


def require_voice_for(code: str) -> str:
    """Raise a helpful error naming the language when no voice is installed."""
    library = PiperVoiceLibrary()
    if library.voices_for_language(code) and not any(
        library.is_installed(v) for v in library.voices_for_language(code)
    ):
        from app.config import language_display_name

        raise AppError(
            f"No {language_display_name(code)} voice is installed. Open the "
            "Voices screen to download one (about 60 MB, then it works offline)."
        )
    return code
