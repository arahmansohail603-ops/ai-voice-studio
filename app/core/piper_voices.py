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
import http.client
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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

#: Hugging Face throttles anonymous downloads to roughly 1 MB/s, so the full
#: catalog is a multi-hour transfer. Resuming is therefore not a nicety: without
#: it a single dropped connection can cost an hour of re-downloading, which is
#: exactly the "it keeps downloading the same voice again" complaint.
#: Backoff between attempts to finish one file, in seconds. A connection that
#: dies mid-transfer is usually transient, so a few patient tries finish the job.
_RETRY_BACKOFF_SECONDS = (2.0, 6.0, 15.0)

STAGE_DOWNLOADING = "downloading"
STAGE_VERIFYING = "verifying"
STAGE_INSTALLED = "installed"
#: A transfer was interrupted and is about to be retried from where it stopped.
STAGE_RETRYING = "retrying"

ProgressCallback = Callable[[str, int, int, str], None]
#: Polled while bytes are moving; return True to stop. Everything already
#: finished stays on disk, so the next run picks up from the same place.
CancelCallback = Callable[[], bool]

#: Failures that mean "the connection broke", not "the server said no".
#: :class:`http.client.IncompleteRead` is the important one: a response that
#: stops early raises it, and it is neither an :class:`OSError` nor a
#: :class:`ValueError`, so without it a dropped transfer would escape the resume
#: logic and lose the partial file.
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
    ValueError,
)


class _StreamCut(Exception):
    """The transfer stopped early, but the bytes already on disk are reusable."""


class _Aborted(Exception):
    """The caller asked to stop. Whatever downloaded so far is kept."""


@dataclass
class BulkInstallResult:
    """What an :meth:`PiperVoiceLibrary.install_all` run achieved."""

    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled

    def summary(self) -> str:
        if self.cancelled:
            return f"Stopped after {len(self.installed)} voices."
        if self.failed:
            first = self.failed[0]
            return (
                f"{len(self.installed)} installed, {len(self.failed)} failed. "
                f"First failure: {first[0]} -- {first[1]}"
            )
        return f"All {len(self.installed) + len(self.skipped)} voices are ready."


def _gb(size: int) -> str:
    return f"{size / (1024 ** 3):.1f}"


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
        # Filename -> md5 for every published asset. Built once: resolving a
        # digest per file used to re-read and re-scan the whole 245 KB catalog,
        # which is a few hundred wasted parses over a full prefetch.
        self._digests: dict[str, str] | None = None

    # -------------------------------------------------------------- catalog
    @property
    def index_path(self) -> Path:
        return self.voice_dir / "voices.json"

    def installed(self, snapshot: set[str] | None = None) -> set[str]:
        """Stems of the voices already on disk and complete.

        Passing an existing snapshot is cheaper than globbing when checking a
        hundred voices in one pass.
        """
        if snapshot is not None:
            return snapshot
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

    def is_installed(self, entry: VoiceEntry, snapshot: set[str] | None = None) -> bool:
        return entry.installed_name() in self.installed(snapshot=snapshot)

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
        self._digests = None

    def _fetch_index(self) -> dict:
        request = urllib.request.Request(
            VOICES_INDEX_URL,
            headers={"Accept": "application/json", "User-Agent": self._user_agent()},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read(_MAX_INDEX_BYTES + 1)
        except _TRANSPORT_ERRORS as exc:
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
        should_cancel: CancelCallback | None = None,
        snapshot: set[str] | None = None,
    ) -> Path:
        """Download and verify a voice. Returns the installed ``.onnx`` path.

        Already-complete voices return immediately, so a retried download costs
        nothing, and each file is checked on its own: a voice whose model landed
        but whose config did not is finished off with just the config instead of
        re-fetching the whole model.
        """
        if self.is_installed(entry, snapshot):
            return self.voice_dir / f"{entry.installed_name()}.onnx"

        wanted = [p for p in entry.files if p.endswith((".onnx", ".onnx.json"))]
        if not wanted:
            raise TTSGenerationError(
                f"The catalog entry for '{entry.key}' lists no downloadable files."
            )
        self.voice_dir.mkdir(parents=True, exist_ok=True)

        def needed(remote: str) -> bool:
            return not self._is_complete(
                self.voice_dir / Path(remote).name, entry._size(remote)
            )

        # Config first, model second: a voice without its config is not
        # speakable, whereas the model on its own is harmless.
        pending = sorted(wanted, key=lambda p: p.endswith(".onnx"))
        pending = [p for p in pending if needed(p)]
        total = sum(entry._size(p) for p in pending)

        received = 0
        for remote in pending:
            size = entry._size(remote)
            self._fetch_file(
                remote,
                self.voice_dir / Path(remote).name,
                size,
                on_progress,
                received=received,
                total=total,
                should_cancel=should_cancel,
            )
            received += size
        if on_progress:
            on_progress(STAGE_INSTALLED, total, total, entry.installed_name())
        return self.voice_dir / f"{entry.installed_name()}.onnx"

    def _is_complete(self, destination: Path, expected_bytes: int) -> bool:
        """True when *destination* already holds the bytes the catalog promised.

        This is what makes a prefetch idempotent: without it, re-running a
        download would refetch gigabytes that are already correct on disk.
        """
        try:
            size = destination.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        if expected_bytes and size != expected_bytes:
            return False
        expected_md5 = self._expected_md5(destination.name)
        if expected_md5 and self._md5(destination) != expected_md5:
            return False
        return True

    def _fetch_file(
        self,
        remote: str,
        destination: Path,
        expected_bytes: int,
        on_progress: ProgressCallback | None,
        received: int = 0,
        total: int = 0,
        should_cancel: CancelCallback | None = None,
    ) -> None:
        """Stream one file, verify it, then move it into place atomically.

        A transfer that dies part-way keeps its ``.part`` file and is resumed
        with a Range request, because at the throttled speed this server
        delivers, starting over can mean another hour for one voice.
        """
        if expected_bytes > _MAX_FILE_BYTES:
            raise TTSGenerationError(
                f"'{destination.name}' is implausibly large "
                f"({expected_bytes} bytes); refusing to download it."
            )
        if self._is_complete(destination, expected_bytes):
            return

        self.voice_dir.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".part")
        attempts = len(_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            have = self._resumable_size(temp, expected_bytes)
            try:
                got = self._stream(
                    remote, temp, destination.name, expected_bytes,
                    on_progress, received, total, have, should_cancel,
                )
            except (_Aborted, _StreamCut) as exc:
                if isinstance(exc, _Aborted) or attempt >= attempts - 1:
                    if isinstance(exc, _StreamCut):
                        raise NetworkError(
                            f"Could not download '{destination.name}': {exc}"
                        ) from exc
                    raise
                # Keep the partial file and pick up where it stopped.
                landed = self._resumable_size(temp, expected_bytes)
                if on_progress:
                    on_progress(
                        STAGE_RETRYING, received + landed,
                        total or expected_bytes, destination.name,
                    )
                self._wait_before_retry(attempt, should_cancel)
                continue
            except NetworkError:
                if attempt >= attempts - 1:
                    raise
                if on_progress:
                    on_progress(
                        STAGE_RETRYING, received + have,
                        total or expected_bytes, destination.name,
                    )
                self._wait_before_retry(attempt, should_cancel)
                continue

            # The stream ended on its own, so any size or digest mismatch is a
            # fact about the server rather than a dropped connection: fail
            # without retrying, and do not leave the stub behind.
            if expected_bytes and got != expected_bytes:
                temp.unlink(missing_ok=True)
                raise TTSGenerationError(
                    f"'{destination.name}' was {got} bytes but the catalog says "
                    f"{expected_bytes}. The download was incomplete."
                )
            if on_progress:
                on_progress(
                    STAGE_VERIFYING, received + got, total or got, destination.name
                )
            self._verify(temp, destination)
            os.replace(temp, destination)
            return

    @staticmethod
    def _wait_before_retry(attempt: int, should_cancel: CancelCallback | None) -> None:
        """Pause before retrying, in slices, so Stop stays responsive."""
        remaining = _RETRY_BACKOFF_SECONDS[attempt]
        while remaining > 0:
            if should_cancel and should_cancel():
                raise _Aborted()
            nap = min(0.25, remaining)
            time.sleep(nap)
            remaining -= nap

    def _resumable_size(self, temp: Path, expected_bytes: int) -> int:
        """Bytes already on disk that a Range request can continue from."""

        try:
            have = temp.stat().st_size
        except OSError:
            return 0
        if expected_bytes and have >= expected_bytes:
            # A partial at or past the published size cannot be continued. Either
            # a previous attempt appended something wrong or the server moved;
            # either way, start this file again.
            temp.unlink(missing_ok=True)
            return 0
        return have

    def _stream(
        self,
        remote: str,
        temp: Path,
        label: str,
        expected_bytes: int,
        on_progress: ProgressCallback | None,
        received: int,
        total: int,
        have: int,
        should_cancel: CancelCallback | None,
    ) -> int:
        """Pull one file into *temp*, returning how many bytes the file now has."""
        url = _FILE_BASE + remote
        headers = {"User-Agent": self._user_agent()}
        resuming = have > 0
        if resuming:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and resuming:
                # Our partial is past the end of the real file: drop it and
                # fetch this file from the beginning.
                temp.unlink(missing_ok=True)
                return self._stream(
                    remote, temp, label, expected_bytes, on_progress,
                    received, total, 0, should_cancel,
                )
            raise NetworkError(
                f"Could not download '{label}': the server replied "
                f"HTTP {exc.code} {exc.reason}"
            ) from exc
        except _TRANSPORT_ERRORS as exc:
            raise _StreamCut(exc) from exc

        got = have
        try:
            with response:
                status = getattr(response, "status", None) or response.getcode()
                if resuming and status != 206:
                    # The server ignored the Range header and is sending the whole
                    # file again. Appending to what we have would corrupt it.
                    got = 0
                    mode = "wb"
                else:
                    mode = "ab" if resuming else "wb"
                # read1() rather than read(): read(n) is happy to return fewer
                # bytes than it promised without complaining, so a body that
                # stops early looks like a clean end-of-file. What is still owed
                # is checked below instead.
                read1 = getattr(response, "read1", None) or response.read
                with open(temp, mode) as sink:
                    while True:
                        if should_cancel and should_cancel():
                            raise _Aborted()
                        chunk = read1(_CHUNK)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > _MAX_FILE_BYTES:
                            raise TTSGenerationError(
                                f"'{label}' downloaded far larger than "
                                "expected; the download was stopped."
                            )
                        sink.write(chunk)
                        if on_progress:
                            on_progress(
                                STAGE_DOWNLOADING,
                                received + got,
                                total or expected_bytes,
                                label,
                            )
                owed = getattr(response, "length", None)
                if owed:
                    # The server had more to send and the connection ended
                    # anyway. That is a dropped transfer, not a short file, so
                    # the bytes already written are kept and resumed. This is
                    # the common shape of failure behind a CDN or proxy.
                    raise _StreamCut(
                        f"the connection closed with {owed} bytes still to send"
                    )
        except _Aborted:
            raise
        except TTSGenerationError:
            raise
        except _TRANSPORT_ERRORS as exc:
            # The connection died mid-file. The bytes written so far are valid,
            # so keep them and let the caller resume.
            raise _StreamCut(exc) from exc
        return got

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
        if self._digests is None:
            digests: dict[str, str] = {}
            for item in (self._read_cached_index() or {}).values():
                for remote, meta in (item.get("files") or {}).items():
                    if not isinstance(meta, dict):
                        continue
                    digest = meta.get("md5_digest")
                    # First writer wins, matching the old linear scan.
                    if digest and Path(remote).name not in digests:
                        digests[Path(remote).name] = str(digest)
            self._digests = digests
        return self._digests.get(filename)

    @staticmethod
    def _md5(path: Path) -> str:
        digest = hashlib.md5()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _missing_bytes(self, entry: VoiceEntry) -> int:
        """Bytes still needed before *entry* is complete and usable."""
        total = 0
        for remote in entry.files:
            if not remote.endswith((".onnx", ".onnx.json")):
                continue
            if not self._is_complete(
                self.voice_dir / Path(remote).name, entry._size(remote)
            ):
                total += entry._size(remote)
        return total

    # --------------------------------------------------------- bulk prefetch
    def install_all(
        self,
        entries: Iterable[VoiceEntry] | None = None,
        on_progress: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
        free_bytes: int | None = None,
    ) -> BulkInstallResult:
        """Install every published voice, skipping whatever is already there.

        One voice failing does not abandon the other hundred and seventy, and
        stopping part-way keeps every voice finished so far: re-running this
        costs nothing and continues from the first missing file.
        """
        pool = list(self.catalog() if entries is None else entries)
        if not pool:
            raise NetworkError(
                "The Piper voice catalog is empty, so there is nothing to download."
            )
        self.voice_dir.mkdir(parents=True, exist_ok=True)

        result = BulkInstallResult()
        known = self.installed()
        plan: list[tuple[VoiceEntry, int]] = []
        for entry in pool:
            if self.is_installed(entry, known):
                result.skipped.append(entry.installed_name())
            else:
                plan.append((entry, self._missing_bytes(entry)))
        if not plan:
            return result

        grand_total = sum(need for _, need in plan)
        if free_bytes is None:
            try:
                free_bytes = shutil.disk_usage(self.voice_dir).free
            except OSError:
                free_bytes = None
        if free_bytes is not None and free_bytes < grand_total:
            raise TTSGenerationError(
                f"Downloading every voice needs {_gb(grand_total)} GB of free disk "
                f"space, but only {_gb(free_bytes)} GB is available. Free up some "
                "space, or install the languages you actually need from the "
                "Voices screen."
            )

        done = 0
        for index, (entry, need) in enumerate(plan, start=1):
            if should_cancel and should_cancel():
                result.cancelled = True
                break

            def report(stage: str, got: int, _total: int, _label: str,
                       _done: int = done, _entry: VoiceEntry = entry,
                       _index: int = index) -> None:
                if on_progress:
                    on_progress(
                        stage,
                        _done + got,
                        grand_total,
                        f"{_entry.installed_name()} ({_index}/{len(plan)})",
                    )

            try:
                self.install(entry, on_progress=report,
                             should_cancel=should_cancel, snapshot=known)
            except _Aborted:
                result.cancelled = True
                break
            except AppError as exc:
                result.failed.append((entry.installed_name(), str(exc)))
            else:
                result.installed.append(entry.installed_name())
                done += need
                known = self.installed()
        return result

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
        self._digests = None

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

    def remaining(self, entries: Iterable[VoiceEntry] | None = None) -> tuple[int, int]:
        """``(voices still to fetch, bytes still to fetch)`` for the whole catalog.

        Used by the UI to label the bulk button honestly rather than promising
        a download that is already on disk.
        """
        pool = list(self.catalog_from_cache() if entries is None else entries)
        if not pool:
            return 0, 0
        known = self.installed()
        count = 0
        total = 0
        for entry in pool:
            if self.is_installed(entry, known):
                continue
            count += 1
            total += self._missing_bytes(entry)
        return count, total

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
