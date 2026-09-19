"""Microphone recorder built on sounddevice + numpy.

Provides record / pause / resume / stop, a live level meter, elapsed time and
WAV/MP3 saving. No dependency on PyAudio.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:

    class _MissingNumpy:
        """Raises a friendly error on first use instead of crashing at import."""

        def __getattr__(self, name):
            raise MissingDependencyError("numpy", "numpy")

    np = _MissingNumpy()  # type: ignore[assignment]

from app.core import audio_utils
from app.core.errors import (
    DeviceError,
    MicPermissionError,
    MissingDependencyError,
    soft_import,
)


def list_input_devices() -> list[dict]:
    """Query available input devices (or raise DeviceError)."""
    sd = soft_import("sounddevice")
    if sd is None:
        raise MissingDependencyError("sounddevice", "sounddevice")
    try:
        devices = sd.query_devices()
        inputs = []
        default_index = sd.default.device[0] if sd.default.device else None
        for idx, _dev in enumerate(devices):
            info = sd.query_devices(idx)
            if info.get("max_input_channels", 0) > 0:
                inputs.append(
                    {
                        "index": idx,
                        "name": info.get("name", f"Device {idx}"),
                        "default": idx == default_index,
                        "channels": int(info["max_input_channels"]),
                        "samplerate": int(info.get("default_samplerate", 44100)),
                    }
                )
        return inputs
    except Exception as exc:
        raise DeviceError(f"Could not query audio devices. {exc}") from exc


def default_input_device() -> int | None:
    try:
        sd = soft_import("sounddevice")
        if sd is None:
            return None
        dev = sd.query_devices(kind="input")
        return int(dev["index"])
    except Exception:
        return None


class Recorder:
    """Thread-safe microphone recorder with pausable input stream."""

    def __init__(self, samplerate: int = 44100, channels: int = 1, device: int | None = None):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self.level_queue: queue.Queue = queue.Queue(maxsize=1)

        self._stream = None
        self._buffers: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._paused = False
        self._samples: np.ndarray | None = None

        self._run_start = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0

    # ------------------------------------------------------------ microphone
    @property
    def has_microphone(self) -> bool:
        try:
            return default_input_device() is not None
        except Exception:
            return False

    def check_microphone(self, raise_error: bool = True) -> bool:
        device = default_input_device()
        if device is None:
            if raise_error:
                raise MicPermissionError(
                    "No microphone input device was found.\n\nOn Windows check: "
                    "Settings > Privacy > Microphone > 'Let desktop apps access "
                    "your microphone' is ON, then restart AI Voice Studio."
                )
            return False
        return True

    # ------------------------------------------------------------ recording
    def start(self) -> None:
        self.check_microphone()
        sd = soft_import("sounddevice")
        if sd is None:
            raise MissingDependencyError("sounddevice", "sounddevice")
        if self._recording:
            return

        self._buffers = []
        self._recording = True
        self._paused = False
        self._paused_at = None
        self._paused_total = 0.0
        self._run_start = time.monotonic()
        self._samples = None

        device = self.device if self.device is not None else default_input_device()
        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                device=device,
                blocksize=int(0.05 * self.samplerate),
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._recording = False
            raise MicPermissionError(
                "Could not open the microphone. It may be in use by another "
                f"app or blocked by privacy settings. ({exc})"
            ) from exc

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if self._paused or not self._recording:
            return
        block = np.copy(indata)
        with self._lock:
            self._buffers.append(block)
        level = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        try:
            self.level_queue.put_nowait(level)
        except queue.Full:
            pass

    def pause(self) -> None:
        if self._recording and not self._paused:
            self._paused = True
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._recording and self._paused:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self._paused = False

    def stop(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._paused = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
        self._paused_at = None

        with self._lock:
            if self._buffers:
                self._samples = np.concatenate(self._buffers, axis=0).astype(np.float32)
            else:
                self._samples = np.zeros((0, self.channels), dtype=np.float32)

    def clear(self) -> None:
        self._samples = None
        self._buffers = []

    # -------------------------------------------------------------- state
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def capture(self) -> np.ndarray | None:
        return self._samples

    def elapsed(self) -> float:
        """Seconds of audio actually captured (excluding pauses)."""
        if not self._recording and not self._buffers:
            return 0.0
        now = time.monotonic()
        if self._recording:
            total = (now - self._run_start) - self._paused_total
            if self._paused_at is not None:
                total -= now - self._paused_at
            return max(0.0, total)
        return self._buffers_duration()

    _buffers_cache: float = 0.0

    def _buffers_duration(self) -> float:
        with self._lock:
            total = sum(b.shape[0] for b in self._buffers)
        return total / self.samplerate

    # -------------------------------------------------------------- saving
    def save(self, path: str | Path, fmt: str = "wav") -> Path:
        if self._samples is None:
            raise DeviceError("Nothing recorded yet.")
        return audio_utils.save_recording(self._samples, self.samplerate, path, fmt)
