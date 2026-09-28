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
    """Capture devices worth offering in Settings, speakers removed.

    Anything with ``max_input_channels > 0`` is returned by PortAudio, which on
    Windows includes "PC Speaker" and "Stereo Mix" entries. Offering those is how
    a user ends up recording from a speaker and getting error 9996 with no
    explanation, so they are dropped -- unless that would leave nothing to show.
    """
    sd = soft_import("sounddevice")
    if sd is None:
        raise MissingDependencyError("sounddevice", "sounddevice")
    try:
        default_index = sd.default.device[0] if sd.default.device else None
        candidates: list[dict] = []
        for idx, _dev in enumerate(sd.query_devices()):
            if int(_dev.get("max_input_channels", 0)) <= 0:
                continue
            info = device_info(idx)
            if info is None:
                continue
            info["default"] = idx == default_index
            candidates.append(info)

        usable = [d for d in candidates if not d["is_output"]]
        devices = usable or candidates
        # A failing device is worth showing anyway, and defaults come first.
        devices.sort(key=lambda d: (not d["default"], d["name"].lower()))
        return devices
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


# Windows reports an output endpoint as an input device whenever the driver
# gives it more than zero ``max_input_channels``. A Realtek "PC Speaker" or a
# "Stereo Mix" is not a microphone, but it passes every PortAudio capability
# check -- ``check_input_settings`` reports it as OPEN -- and only fails when
# the stream is actually opened, with the useless MMSYSERR_INVALPARAM (9996).
# So the names are the only signal available before trying to record.
_OUTPUT_DEVICE_HINTS = (
    "speaker",
    "stereo mix",
    "loopback",
    "output with hap",
    "cable output",
    "hdmi",
    "virtual audio",
    "sound mapper - output",
)

# Rates tried when opening a device, best first. Native first because it needs
# no resampling, then whatever the user asked for, then the common defaults.
# A fixed rate is what breaks mics: a 48 kHz USB interface cannot be opened at
# 44100, and a Bluetooth headset is often 8 or 16 kHz.
_FALLBACK_RATES = (48000, 44100, 22050, 16000)


def looks_like_output_device(name: str) -> bool:
    """True if *name* is almost certainly a speaker/loopback, not a microphone."""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in _OUTPUT_DEVICE_HINTS)


def device_info(index: int | None) -> dict | None:
    """PortAudio's record for one device, or None if it cannot be read."""
    sd = soft_import("sounddevice")
    if sd is None:
        return None
    try:
        if index is None:
            dev = sd.query_devices(kind="input")
        else:
            dev = sd.query_devices(int(index))
        if int(dev.get("max_input_channels", 0)) <= 0:
            return None
        return {
            "index": int(dev["index"]),
            "name": str(dev.get("name", f"Device {index}")),
            "channels": int(dev["max_input_channels"]),
            "samplerate": int(round(float(dev.get("default_samplerate", 0) or 0))),
            "hostapi": _hostapi_name(dev),
            "is_output": looks_like_output_device(str(dev.get("name", ""))),
        }
    except Exception:
        return None


def _hostapi_name(device: dict) -> str:
    sd = soft_import("sounddevice")
    try:
        return str(sd.query_hostapis(int(device["hostapi"]))["name"])
    except Exception:
        return ""


def device_native_rate(index: int | None) -> int | None:
    """The rate the device itself prefers, or None if it will not say."""
    info = device_info(index)
    rate = info.get("samplerate") if info else 0
    return int(rate) if rate else None


def candidate_rates(native: int | None, preferred: int | None) -> list[int]:
    """Ordered, de-duplicated rates to try when opening *a* device."""
    rates: list[int] = []
    for rate in (native, preferred, *_FALLBACK_RATES):
        try:
            value = int(rate)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in rates:
            rates.append(value)
    return rates or [44100]


# MMSYSERR_INVALPARAM. Raised by waveInOpen/WASAPI for a bad format, a rate the
# device does not do, or an endpoint that cannot capture at all -- it does not
# distinguish them, so it is only used to pick a better sentence.
_FORMAT_ERROR_CODES = ("9996", "0x270c", "invalid parameter", "invalid sample rate")


def _is_format_error(error: Exception | None) -> bool:
    if error is None:
        return False
    text = str(error).lower()
    return any(code in text for code in _FORMAT_ERROR_CODES)


def _is_in_use_error(error: Exception | None) -> bool:
    if error is None:
        return False
    text = str(error).lower()
    return any(
        code in text
        for code in ("10035", "in use", "already open", "device unavailable", "-9999")
    )


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
        if self.device is not None and self.device != device:
            info = device_info(self.device)
            if info is not None and info.get("is_output"):
                # A saved speaker is a dead end: every recording would fail with
                # error 9996. Say so up front instead of at press-to-record.
                if raise_error:
                    raise MicPermissionError(
                        f"The saved input device '{info['name']}' is an output "
                        "device, not a microphone, so recording will fail. "
                        "Change it under Settings > Microphone > Input device."
                    )
                return False
        return True

    def _open_failure_message(
        self, device: int | None, rates: list[int], error: Exception | None
    ) -> str:
        """Explain a failed microphone open, naming the actual cause.

        Windows reports every one of these the same way -- a bare "9996"
        (MMSYSERR_INVALPARAM) from waveInOpen -- whether the real problem is a
        speaker masquerading as a microphone, a sample rate the hardware
        refuses, or a genuine privacy block. Blaming "privacy settings" for all
        three is what made this unfixable from the user's side, so each cause
        gets its own sentence.
        """
        info = device_info(device) or {}
        name = info.get("name") or "the selected device"
        tried = ", ".join(f"{r} Hz" for r in rates)
        where = "Settings > Microphone > Input device"

        lines = [f"Could not open the microphone '{name}'."]

        if info.get("is_output"):
            lines.append(
                "That entry is an output device, such as a speaker or a stereo "
                "mix, so it cannot record. Pick an entry named like a "
                "microphone or headset instead."
            )
        elif error is not None and _is_format_error(error):
            lines.append(
                f"The device rejected every sample rate tried ({tried}). A USB "
                "interface or headset often only accepts its own rate; leaving "
                "Input device on 'default' usually fixes this."
            )
        elif error is not None and _is_in_use_error(error):
            lines.append(
                "The microphone is already in use by another application. Close "
                "call, chat, or recording software and try again."
            )
        else:
            lines.append(
                "Check that Windows allows desktop apps to use the microphone "
                "(Settings > Privacy > Microphone), then restart the app."
            )

        lines.append(f"Change the device under {where}. The full error was: {error}")
        return " ".join(lines)

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
        rates = candidate_rates(device_native_rate(device), self.samplerate)
        last_error: Exception | None = None
        stream = None
        for rate in rates:
            try:
                stream = sd.InputStream(
                    samplerate=rate,
                    channels=self.channels,
                    device=device,
                    blocksize=max(1, int(0.05 * rate)),
                    callback=self._on_audio,
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001 - try the next rate
                last_error = exc
                if stream is not None:
                    # PaInputStream allocated device state; leaking one per
                    # candidate rate would exhaust handles on a bad device.
                    try:
                        stream.close()
                    except Exception:
                        pass
                stream = None
                continue
            # The stream really runs at `rate`, not the one requested, and
            # save()/_buffers_duration() divide by self.samplerate. Leaving the
            # old value here would write every recording at the wrong speed and
            # report the wrong length.
            self.samplerate = rate
            self._stream = stream
            break
        else:
            self._recording = False
            self._buffers = []
            raise MicPermissionError(self._open_failure_message(device, rates, last_error))

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
