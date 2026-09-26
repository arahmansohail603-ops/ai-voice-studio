"""Speech-to-text engine — fully offline (Vosk).

Captures the microphone with ``sounddevice`` on a dedicated capture thread so
the audio stream is *never* paused while recognition runs. Phrase segments are
queued and recognised on a separate worker thread by the local **Vosk** engine
(no internet, no API key). An optional ``whisper`` backend is offered when the
package is installed.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

from app.config import VOSK_MODEL_DIR
from app.core.errors import (
    AppError,
    MicPermissionError,
    MissingDependencyError,
    module_installed,
    soft_import,
)
from app.core.recorder import default_input_device
from app.core.vosk_engine import VoskModelManager

try:
    import numpy as np
except ImportError:

    class _MissingNumpy:
        def __getattr__(self, name):
            raise MissingDependencyError("numpy", "numpy")

    np = _MissingNumpy()  # type: ignore[assignment]

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
ROLLING_SECONDS = 3.0
SILENCE_SECONDS = 0.7
MAX_PHRASE_SECONDS = 12.0
MIN_PHRASE_SECONDS = 0.5

# VAD thresholds in int16 RMS units (32767 = full scale)
ABSOLUTE_FLOOR = 500.0
FLOOR_RATIO = 2.0
FLOOR_DELTA = 400.0

_LEVEL_MAX = 32000.0

# Whisper identifies languages by *bare* ISO 639-1 code. The app's own codes are
# already ISO 639-1, but a saved setting can carry a regional tag such as "ur-PK"
# or "en-US", and Whisper answers anything that is not a plain code with
# "ValueError: Unsupported language: ur-pk" from deep inside recognition. The
# tag therefore has to be stripped before the call.
WHISPER_LANGUAGE_ALIASES = {
    # Whisper ships a single Norwegian model under "no"; the app uses the
    # Bokmal code "nb", which it does not recognise.
    "nb": "no",
}

# App language codes that Whisper genuinely has no model for. This is a static
# list on purpose: asking Whisper costs an import that pulls in torch (~5s),
# and the answer is needed every time the engine list is refreshed.
WHISPER_UNSUPPORTED = frozenset({"eo", "ga", "ky", "pb", "zt"})


class WhisperUnsupportedError(AppError):
    """Raised when Whisper has no model for the selected language."""


def whisper_language(code: str) -> str:
    """Normalise an app language code to one Whisper accepts.

    Raises :class:`WhisperUnsupportedError` when Whisper has no model for the
    language, so the user gets a sentence naming the language instead of a bare
    "Unsupported language" from the middle of the recognition stack.
    """
    base = (code or "").strip().replace("_", "-").split("-")[0].lower()
    base = WHISPER_LANGUAGE_ALIASES.get(base, base)
    if not base or base in WHISPER_UNSUPPORTED:
        from app.config import language_display_name

        name = language_display_name(base) or (code or "that language")
        raise WhisperUnsupportedError(
            f"Whisper has no model for {name} ({code}). "
            "Pick a different language, or install a Vosk model from the "
            "Models screen if one exists for it."
        )
    return base


class NoSpeechError(AppError):
    """Raised when the recogniser heard audio but no speech."""


class STTEngine:
    """Live microphone transcription with pluggable recognition backends."""

    #: Whisper model size used for live recognition. "base" is Whisper's own
    #: default and the smallest multilingual model that still handles Urdu and
    #: Hindi recognisably; it is downloaded once (~142 MB) and then cached.
    whisper_model = "base"

    def __init__(self, language: str = "en-US", engine: str = "vosk"):
        self.language = language
        self.engine = engine
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue()
        self._noise_floor = ABSOLUTE_FLOOR
        self._vosk = VoskModelManager(VOSK_MODEL_DIR, language="en")

    # ------------------------------------------------------------------ mic
    @property
    def has_microphone(self) -> bool:
        return default_input_device() is not None

    def check_microphone(self, raise_error: bool = True) -> bool:
        if default_input_device() is None:
            if raise_error:
                raise MicPermissionError(
                    "No microphone input device was found. Check Windows "
                    "Privacy > Microphone settings and restart the app."
                )
            return False
        return True

    # ------------------------------------------------------------- engines
    def available_engines(self) -> list[str]:
        """List recognition backends that are ready to use right now.

        A backend is only offered when its Python package is installed *and* its
        model is already on disk, so the user can never start a recording that
        is guaranteed to fail. Vosk additionally needs a model for the selected
        language.
        """
        engines: list[str] = []
        if module_installed("vosk") and self._vosk.supported_installed(self.language):
            engines.append("vosk")
        if module_installed("whisper") and self._whisper_usable():
            engines.append("whisper")
        return engines

    def _whisper_usable(self) -> bool:
        """True when Whisper has a model for the selected language."""
        try:
            whisper_language(self.language)
        except WhisperUnsupportedError:
            return False
        return True

    def is_engine_ready(self, engine: str) -> bool:
        """True when ``engine`` can run for the current language."""
        return engine in self.available_engines()

    def engine_status(self, engine: str) -> str:
        """Human-readable reason why an engine is or is not available."""
        if engine == "vosk":
            if not module_installed("vosk"):
                return "The 'vosk' package is not installed."
            if not self._vosk.supported_installed(self.language):
                return self._vosk.catalog_status(self.language)
            return self._vosk.model_info(self.language)
        if engine == "whisper":
            if not module_installed("whisper"):
                return "The 'whisper' package is not installed."
            if not self._whisper_usable():
                try:
                    whisper_language(self.language)
                except WhisperUnsupportedError as exc:
                    return str(exc)
            return f"Ready (uses the '{self.whisper_model}' model)"
        return f"Unknown speech-recognition engine '{engine}'."

    def _recognize_offline(self, audio, on_status=None) -> str:
        text = self._vosk.recognize(audio, self.language)
        return (text or "").strip()

    def _recognize(self, audio, on_status=None) -> str:
        if self.engine == "whisper":
            sr = soft_import("speech_recognition")
            if sr is None:
                raise MissingDependencyError("speech_recognition", "SpeechRecognition")
            language = whisper_language(self.language)
            try:
                return sr.Recognizer().recognize_whisper(
                    audio, model=self.whisper_model, language=language
                )
            except Exception as exc:
                raise AppError(f"Whisper recognition failed: {exc}") from exc

        if not module_installed("vosk"):
            raise MissingDependencyError(
                "vosk", "vosk",
                detail="Offline recognition needs Vosk:  pip install vosk",
            )
        # Raises ModelNotInstalledError when no model for this language is
        # installed; the UI catches that and offers the Models screen.
        text = self._recognize_offline(audio, on_status)
        if not text:
            raise NoSpeechError("No speech detected in that segment.")
        return text

    # --------------------------------------------------------------- live
    def start_listening(
        self,
        on_text: Callable[[str], None],
        on_status: Callable[[str], None],
        on_error: Callable[[Exception], None],
        on_level: Callable[[float], None] | None = None,
    ) -> bool:
        """Begin listening in the background. Returns False if mic busy/already running."""
        if self._thread is not None and self._thread.is_alive():
            return False
        if not self.check_microphone(raise_error=False):
            on_error(
                MicPermissionError(
                    "No microphone found. Enable microphone access in Windows "
                    "privacy settings and restart."
                )
            )
            return False

        self._stop_event.clear()
        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(on_status, on_level, on_error),
            name="stt-capture",
            daemon=True,
        )
        self._worker = threading.Thread(
            target=self._recognition_loop,
            args=(on_text, on_status, on_error),
            name="stt-recognizer",
            daemon=True,
        )
        self._thread.start()
        self._worker.start()
        return True

    def stop_listening(self) -> None:
        self._stop_event.set()

    @property
    def is_listening(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def model_info(self) -> str:
        return self._vosk.model_info(self.language)

    # ------------------------------------------------------------ capture
    def _capture_loop(self, on_status, on_level, on_error) -> None:
        sd = soft_import("sounddevice")
        if sd is None:
            on_error(MissingDependencyError("sounddevice", "sounddevice"))
            return

        device = default_input_device()
        blocksize = int(SAMPLE_RATE * BLOCK_SECONDS)
        silence_blocks = int(SILENCE_SECONDS / BLOCK_SECONDS)
        max_blocks = int(MAX_PHRASE_SECONDS / BLOCK_SECONDS)
        rolling_blocks = int(ROLLING_SECONDS / BLOCK_SECONDS)
        min_blocks = int(MIN_PHRASE_SECONDS / BLOCK_SECONDS)
        try:
            on_status("Calibrating for ambient noise…")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=device,
                blocksize=blocksize,
            ) as stream:
                samples = []
                for _ in range(int(1.0 / BLOCK_SECONDS)):
                    block, _ = stream.read(blocksize)
                    samples.append(block)
                calib = np.concatenate(samples)
                self._noise_floor = max(
                    float(np.sqrt((calib.astype(np.float32) ** 2).mean())),
                    ABSOLUTE_FLOOR,
                )

                phrase: list[np.ndarray] = []
                phrase_blocks = 0
                silent_blocks = 0
                speech_seen = False

                on_status("Listening…")
                while not self._stop_event.is_set():
                    block, _ = stream.read(blocksize)
                    frame = block.astype(np.float32)
                    rms = float(np.sqrt((frame ** 2).mean()))
                    if on_level:
                        on_level(min(rms / _LEVEL_MAX, 1.0))

                    is_speech = (
                        rms >= ABSOLUTE_FLOOR
                        and rms >= max(self._noise_floor * FLOOR_RATIO,
                                       self._noise_floor + FLOOR_DELTA)
                    )

                    if is_speech:
                        phrase.append(block)
                        phrase_blocks += 1
                        silent_blocks = 0
                        speech_seen = True
                    else:
                        silent_blocks += 1
                        if not phrase:
                            self._noise_floor = round(
                                self._noise_floor * 0.9 + rms * 0.1, 3
                            )

                    if speech_seen and (
                        phrase_blocks >= max_blocks
                        or (
                            silent_blocks >= silence_blocks
                            and phrase_blocks >= min_blocks
                        )
                        or (silent_blocks == 0 and phrase_blocks >= rolling_blocks)
                    ):
                        self._ship(phrase)
                        phrase = []
                        phrase_blocks = 0
                        speech_seen = False

                if speech_seen and phrase_blocks >= min_blocks:
                    self._ship(phrase)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            on_error(exc)

    def _ship(self, phrase: list[np.ndarray]) -> None:
        try:
            from speech_recognition import AudioData as SRAudioData
        except ImportError:
            return
        data = np.concatenate(phrase).tobytes()
        self._queue.put(SRAudioData(data, SAMPLE_RATE, 2))

    # -------------------------------------------------------- recognition
    def _recognition_loop(self, on_text, on_status, on_error) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                audio = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if audio is None:
                continue
            try:
                on_status("Recognising…")
                start = time.perf_counter()
                text = (self._recognize(audio, on_status) or "").strip()
                elapsed = time.perf_counter() - start
            except NoSpeechError:
                on_status("Listening…")
                continue
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                on_error(exc)
                on_status("Listening…")
                continue
            if text:
                on_text(text)
                on_status(f"Listening… (response {elapsed:.1f}s)")
            else:
                on_status("Listening…")