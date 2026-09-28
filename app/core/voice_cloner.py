"""Voice cloning ("My Voice") backend built on Coqui TTS XTTS-v2.

The model is heavy (~2 GB) and only loaded on first request, in a background
thread. If the ``TTS`` package or the model download fails, the caller is told
it can fall back to the offline system voice.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from app.config import XTTS_MODEL_NAME
from app.core.errors import CloneModelError


class CloneState(str, Enum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


# Languages XTTS-v2 officially understands: use these ISO codes when cloning.
XTTS_LANGUAGES = [
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl",
    "cs", "ar", "zh", "ja", "hu", "ko", "hi",
]


# Reference-recording limits. Named so the recording UI and this validator
# cannot drift apart: the UI auto-stops at MAX and refuses anything under MIN.
MIN_SAMPLE_SECONDS: float = 3.0
MAX_SAMPLE_SECONDS: float = 40.0


def validate_reference_sample(
    path: str | Path,
    min_seconds: float = MIN_SAMPLE_SECONDS,
    max_seconds: float = MAX_SAMPLE_SECONDS,
) -> None:
    """Shared sanity checks for a reference voice recording (all clone engines)."""
    from app.core import audio_utils

    if not Path(path).exists():
        raise CloneModelError("Voice sample file not found.")
    try:
        data, rate = audio_utils.read_audio(path)
    except Exception as exc:
        raise CloneModelError(f"Could not read voice sample: {exc}") from exc
    duration = float(len(data)) / float(rate)
    if duration < min_seconds:
        raise CloneModelError(
            f"Voice sample is too short ({duration:.1f}s). Record at least "
            f"{min_seconds:.0f}s for good quality."
        )
    if duration > max_seconds:
        raise CloneModelError(
            f"Voice sample is longer than {max_seconds:.0f}s. Trim it down."
        )
    # simple loudness check to reject silent files
    import numpy as np

    rms = float(np.sqrt(np.mean(data**2)))  # noqa: S101
    if rms < 0.005:
        raise CloneModelError("The voice sample appears to be silent.")


class VoiceCloner:
    """Manages the local XTTS model lifecycle and synthesis."""

    def __init__(self) -> None:
        self._state = CloneState.NOT_LOADED
        self._error = ""
        self._tts = None
        self._device = "cpu"
        self._load_thread: threading.Thread | None = None
        self._synth_thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()

    # --------------------------------------------------------------- state
    @property
    def state(self) -> CloneState:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def device(self) -> str:
        """``"cuda"`` or ``"cpu"`` -- resolved when the model loads."""
        return self._device

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
        except ImportError:
            return False
        try:
            return bool(torch.cuda.is_available())
        except Exception:  # pragma: no cover - a broken driver is not a GPU
            return False

    @property
    def is_loading(self) -> bool:
        return self._state == CloneState.LOADING

    @property
    def is_ready(self) -> bool:
        return self._state == CloneState.READY

    @property
    def available(self) -> bool:
        from app.core.errors import module_installed

        return module_installed("TTS")

    @staticmethod
    def supported_languages() -> list[str]:
        return XTTS_LANGUAGES

    # --------------------------------------------------------------- load
    def load_background(self, on_change: Callable[[CloneState, str], None]) -> None:
        """Begin model loading on a worker thread; ``on_change`` for UI."""
        if self._state == CloneState.LOADING or self.is_ready:
            return

        self._state = CloneState.LOADING
        self._error = ""
        # Resolve the device before announcing, so the user is told where the
        # work will happen instead of discovering it from the speed.
        use_gpu = self._cuda_available()
        self._device = "cuda" if use_gpu else "cpu"
        device_note = "CUDA (GPU)" if use_gpu else "CPU (slow)"
        on_change(
            self._state,
            f"Loading voice-cloning model… first run downloads ~2 GB. "
            f"Runtime: {device_note}.",
        )

        def _load() -> None:
            try:
                from TTS.api import TTS as CoquiTTS  # noqa: N814

                # Must be passed explicitly. This coqui-tts build defaults
                # ``gpu`` to False rather than None, so omitting it pinned every
                # machine -- including ones with a perfectly good NVIDIA GPU --
                # to the CPU. Passing True on a machine with no CUDA would raise
                # instead of falling back, hence the explicit boolean.
                model = CoquiTTS(
                    model_name=XTTS_MODEL_NAME, progress_bar=False, gpu=use_gpu
                )
                with self._sync_lock:
                    self._tts = model
                    self._state = CloneState.READY
                    self._error = ""
            except Exception as exc:
                with self._sync_lock:
                    self._state = CloneState.ERROR
                    self._error = (
                        "Voice cloning model could not be loaded. "
                        f"Install it with `pip install -r requirements-clone.txt`. ({exc})"
                    )
                on_change(self._state, self._error)

            try:
                on_change(self._state, self._error)
            except Exception:
                pass

        self._load_thread = threading.Thread(target=_load, name="xtts-loader", daemon=True)
        self._load_thread.start()

    # ------------------------------------------------------------- synth
    def synthesize(
        self,
        text: str,
        reference_wav: str | Path,
        language: str,
        output_path: str | Path,
        on_change: Callable[[str], None] | None = None,
        on_done: Callable[[Path | None, Exception | None], None] | None = None,
    ) -> None:
        """Synthesize on a worker thread. Thread-safe; only one at a time."""
        if not self.is_ready or self._tts is None:
            raise CloneModelError(
                "The voice-cloning model is not ready yet. Wait for loading "
                "to finish or use a fallback voice."
            )
        if language not in XTTS_LANGUAGES:
            language = "en"

        def _work() -> None:
            try:
                if on_change:
                    on_change("Synthesising with your voice…")
                self._tts.tts_to_file(
                    text=text,
                    speaker_wav=str(reference_wav),
                    language=language,
                    file_path=str(output_path),
                )
                path = Path(output_path)
                if not path.exists() or path.stat().st_size == 0:
                    raise CloneModelError("Clone synthesis produced no audio.")
                if on_done:
                    on_done(path, None)
            except Exception as exc:  # noqa: BLE001
                if on_done:
                    on_done(None, exc)

        self._synth_thread = threading.Thread(target=_work, name="xtts-synth", daemon=True)
        self._synth_thread.start()

    def validate_sample(self, path: str | Path, min_seconds=3.0, max_seconds=40.0) -> None:
        """Basic sanity checks for a reference voice recording."""
        validate_reference_sample(path, min_seconds=min_seconds, max_seconds=max_seconds)
