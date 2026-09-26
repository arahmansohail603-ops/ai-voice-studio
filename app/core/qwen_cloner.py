"""Qwen3-TTS voice cloning backend (``Qwen/Qwen3-TTS-12Hz-1.7B-Base``).

An optional, GPU-first alternative to the Coqui XTTS-v2 cloner. The Base model
clones a voice from a short (3s+) reference clip plus its transcript. The
model and the speech tokenizer (~4.5 GB in total) download into the app's
``models/huggingface`` folder on first use, then work offline.

The public surface mirrors :class:`app.core.voice_cloner.VoiceCloner` so the
My Voice screen can drive either engine interchangeably.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from app.config import QWEN_GPU_ONLY, QWEN_MODEL_NAME
from app.core.errors import CloneModelError, module_available, module_installed
from app.core.voice_cloner import CloneState, validate_reference_sample

# ISO code -> Qwen3-TTS language name (the Base model knows exactly 10).
QWEN_LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}


class QwenVoiceCloner:
    """Manages the local Qwen3-TTS Base model lifecycle and voice cloning."""

    MODEL_NAME = QWEN_MODEL_NAME

    def __init__(self) -> None:
        self._state = CloneState.NOT_LOADED
        self._error = ""
        self._model = None
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
    def is_loading(self) -> bool:
        return self._state == CloneState.LOADING

    @property
    def is_ready(self) -> bool:
        return self._state == CloneState.READY

    @property
    def device(self) -> str:
        return self._device

    @property
    def available(self) -> bool:
        return module_installed("qwen_tts")

    @staticmethod
    def supported_languages() -> list[str]:
        return list(QWEN_LANGUAGE_NAMES)

    @staticmethod
    def language_name(code: str) -> str:
        return QWEN_LANGUAGE_NAMES.get(code or "en", "English")

    # --------------------------------------------------------------- load
    def load_background(self, on_change: Callable[[CloneState, str], None]) -> None:
        """Begin model loading on a worker thread; ``on_change`` for UI."""
        if self._state == CloneState.LOADING or self.is_ready:
            return

        self._state = CloneState.LOADING
        self._error = ""

        if self._cuda_available():
            self._device = "cuda"
            device_note = "CUDA (GPU)"
        elif QWEN_GPU_ONLY:
            # GPU-only engine: refuse CPU so the user is never silently stuck
            # on a slow CPU run instead of using a proper engine.
            self._device = "cpu"
            self._state = CloneState.ERROR
            self._error = (
                "Qwen3-TTS is GPU-only (NVIDIA CUDA) and no NVIDIA GPU was "
                "found on this device, so it will not run on the CPU.\n"
                "To enable it, install the CUDA build of torch:\n"
                "    pip install torch --index-url "
                "https://download.pytorch.org/whl/cu121\n"
                "Otherwise pick a System or Piper voice."
            )
            try:
                on_change(self._state, self._error)
            except Exception:
                pass
            return
        else:
            self._device = "cpu"
            device_note = "CPU (very slow)"
        on_change(
            self._state,
            f"Loading Qwen3-TTS voice-cloning model… first run downloads ~4.5 GB "
            f"to the data folder. Runtime: {device_note}.",
        )

        def _load() -> None:
            try:
                import torch
                from qwen_tts import Qwen3TTSModel

                if self._device == "cuda":
                    dtype = torch.bfloat16
                else:
                    dtype = torch.float32
                attn = "flash_attention_2" if module_available("flash_attn") else "sdpa"
                model = Qwen3TTSModel.from_pretrained(
                    self.MODEL_NAME,
                    device_map=self._device,
                    dtype=dtype,
                    attn_implementation=attn,
                )
                with self._sync_lock:
                    self._model = model
                    self._state = CloneState.READY
                    self._error = ""
            except Exception as exc:  # noqa: BLE001
                with self._sync_lock:
                    self._state = CloneState.ERROR
                    self._error = (
                        "The Qwen3-TTS voice-cloning model could not be loaded. "
                        "It needs an NVIDIA GPU with enough VRAM and the optional "
                        "dependencies. Install them with:\n"
                        "    pip install -r requirements-clone.txt\n"
                        f"({exc})"
                    )
                try:
                    on_change(self._state, self._error)
                except Exception:
                    pass
                return

            try:
                on_change(self._state, self._error)
            except Exception:
                pass

        self._load_thread = threading.Thread(target=_load, name="qwen-loader", daemon=True)
        self._load_thread.start()

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    # ------------------------------------------------------------- synth
    def synthesize(
        self,
        text: str,
        reference_wav: str | Path,
        language: str,
        output_path: str | Path,
        transcript: str = "",
        on_change: Callable[[str], None] | None = None,
        on_done: Callable[[Path | None, Exception | None], None] | None = None,
    ) -> None:
        """Synthesize on a worker thread. Thread-safe; only one at a time."""
        if not self.is_ready or self._model is None:
            raise CloneModelError(
                "The Qwen3-TTS model is not ready yet. Wait for loading to "
                "finish or use a fallback voice."
            )
        lang_name = self.language_name(language or "en")
        transcript = (transcript or "").strip()

        def _work() -> None:
            try:
                if on_change:
                    on_change("Synthesising with your voice (Qwen3-TTS)…")
                import soundfile as sf

                if transcript:
                    wavs, sr = self._model.generate_voice_clone(
                        text=text,
                        language=lang_name,
                        ref_audio=str(reference_wav),
                        ref_text=transcript,
                    )
                else:
                    # No transcript means the speaker embedding alone is used;
                    # cloning quality is slightly reduced but ref_text is optional.
                    wavs, sr = self._model.generate_voice_clone(
                        text=text,
                        language=lang_name,
                        ref_audio=str(reference_wav),
                        x_vector_only_mode=True,
                    )
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(path), wavs[0], sr)
                if not path.exists() or path.stat().st_size == 0:
                    raise CloneModelError("Clone synthesis produced no audio.")
                if on_done:
                    on_done(path, None)
            except Exception as exc:  # noqa: BLE001
                if on_done:
                    on_done(None, exc)

        self._synth_thread = threading.Thread(target=_work, name="qwen-synth", daemon=True)
        self._synth_thread.start()

    @staticmethod
    def validate_sample(
        path: str | Path,
        min_seconds: float = 3.0,
        max_seconds: float = 40.0,
    ) -> None:
        validate_reference_sample(path, min_seconds=min_seconds, max_seconds=max_seconds)