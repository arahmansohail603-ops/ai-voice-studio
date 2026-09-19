"""Speech-to-text engine.

Captures the microphone with ``sounddevice`` (no PyAudio dependency), detects
phrase boundaries by silence, wraps the captured frames into a
``speech_recognition.AudioData`` object and recognises them.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from app.core.recorder import default_input_device

from app.core.errors import (
    AppError,
    MicPermissionError,
    MissingDependencyError,
    NetworkError,
    module_available,
    soft_import,
)

try:
    import numpy as np
except ImportError:

    class _MissingNumpy:
        def __getattr__(self, name):
            raise MissingDependencyError("numpy", "numpy")

    np = _MissingNumpy()  # type: ignore[assignment]

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
SILENCE_SECONDS = 0.9
MAX_PHRASE_SECONDS = 12.0
MIN_PHRASE_SECONDS = 0.5


class STTEngine:
    """Live microphone transcription with pluggable recognition backends."""

    def __init__(self, language: str = "en-US", engine: str = "google"):
        self.language = language
        self.engine = engine
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._noise_floor = 0.003

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
    @staticmethod
    def available_engines() -> list[str]:
        engines = ["google"]
        if module_available("whisper"):
            engines.append("whisper")
        return engines

    def _recognize(self, audio) -> str:
        sr = soft_import("speech_recognition")
        if sr is None:
            raise MissingDependencyError("speech_recognition", "SpeechRecognition")

        if self.engine == "whisper":
            try:
                return sr.Recognizer().recognize_whisper(audio, language=self.language)
            except Exception as exc:
                raise AppError(f"Whisper recognition failed: {exc}") from exc

        # default: Google (online)
        try:
            return sr.Recognizer().recognize_google(audio, language=self.language)
        except sr.RequestError as exc:
            raise NetworkError(
                "Speech recognition requires internet access (Google engine). "
                f"({exc})"
            ) from exc
        except Exception as exc:
            raise AppError(f"Could not recognise speech: {exc}") from exc

    # --------------------------------------------------------------- live
    def start_listening(
        self,
        on_text: Callable[[str], None],
        on_status: Callable[[str], None],
        on_error: Callable[[Exception], None],
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
        self._thread = threading.Thread(
            target=self._listen_loop,
            args=(on_text, on_status, on_error),
            name="stt-listener",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop_listening(self) -> None:
        self._stop_event.set()

    @property
    def is_listening(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def _listen_loop(self, on_text, on_status, on_error) -> None:
        sd = soft_import("sounddevice")
        if sd is None:
            on_error(MissingDependencyError("sounddevice", "sounddevice"))
            return

        device = default_input_device()
        blocksize = int(SAMPLE_RATE * BLOCK_SECONDS)
        try:
            on_status("Calibrating for ambient noise…")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=device,
                blocksize=blocksize,
            ) as stream:
                # ambient calibration
                samples = []
                for _ in range(int(1.0 / BLOCK_SECONDS)):
                    block, _ = stream.read(blocksize)
                    samples.append(block)
                calib = np.concatenate(samples)
                self._noise_floor = float(np.sqrt((calib.astype(np.float32) ** 2).mean())) + 0.002

                buffer = bytearray()
                silent_blocks = 0
                max_blocks = int(MAX_PHRASE_SECONDS / BLOCK_SECONDS)
                min_blocks = int(MIN_PHRASE_SECONDS / BLOCK_SECONDS)
                silence_blocks = int(SILENCE_SECONDS / BLOCK_SECONDS)

                on_status("Listening…")
                while not self._stop_event.is_set():
                    block, _ = stream.read(blocksize)
                    frame = block.astype(np.float32)
                    rms = float(np.sqrt((frame ** 2).mean()))
                    is_silent = rms < self._noise_floor

                    if not is_silent:
                        buffer.extend(block.tobytes())
                        silent_blocks = 0
                    else:
                        silent_blocks += 1

                    buffer_len_blocks = len(buffer) / (2 * SAMPLE_RATE * BLOCK_SECONDS)
                    if buffer and (buffer_len_blocks >= max_blocks or (
                            silent_blocks >= silence_blocks and buffer_len_blocks >= min_blocks)):
                        on_status("Recognising…")
                        try:
                            from speech_recognition import AudioData as SRAudioData

                            audio = SRAudioData(bytes(buffer), SAMPLE_RATE, 2)
                            text = (self._recognize(audio) or "").strip()
                            buffer = bytearray()
                            silent_blocks = 0
                            if text:
                                on_text(text)
                                on_status("Listening…")
                        except Exception as exc:
                            buffer = bytearray()
                            silent_blocks = 0
                            on_status("Listening…")
                            on_error(exc)
        except Exception as exc:
            on_error(exc)
