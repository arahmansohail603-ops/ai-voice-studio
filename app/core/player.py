"""Audio playback built on pygame.mixer.

Supports WAV/MP3 (and anything SDL_mixer decodes). Position tracking is done
manually because the mixer does not expose a reliable absolute playhead.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from app.core.errors import DeviceError, MissingDependencyError, soft_import


class AudioPlayer:
    """Play / pause / resume / stop an audio file, with position tracking."""

    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"

    def __init__(self) -> None:
        self._pygame = None
        self._sound = None
        self._channel = None
        self._state = self.IDLE
        self._path: Optional[Path] = None
        self._duration = 0.0
        self._base = 0.0          # seconds completed before the current run
        self._run_started = 0.0   # monotonic when the current run began
        self._ended = False
        self.on_end: Optional[Callable[[], None]] = None
        self._ensure_backend()

    # ------------------------------------------------------------------ init
    def _ensure_backend(self) -> None:
        pygame = soft_import("pygame")
        if pygame is None:
            raise MissingDependencyError("pygame", "pygame")
        self._pygame = pygame
        try:
            pygame.mixer.init()
        except Exception as exc:
            raise DeviceError(
                "Audio playback is unavailable (no output device found). "
                f"Details: {exc}"
            ) from exc

    # ------------------------------------------------------------------ load
    def load(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Audio file not found: {p}")
        sound = self._pygame.mixer.Sound(str(p))
        self.stop()
        self._sound = sound
        self._path = p
        self._duration = float(sound.get_length())
        self._base = 0.0
        self._ended = False

    # ----------------------------------------------------------------- state
    @property
    def state(self) -> str:
        return self._state

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def duration(self) -> float:
        return self._duration

    def is_active(self) -> bool:
        return self._state in (self.PLAYING, self.PAUSED) and self._sound is not None

    # ----------------------------------------------------------------- play
    def play(self) -> None:
        if self._sound is None:
            return
        if self._state == self.PAUSED:
            self.resume()
            return
        self._channel = self._sound.play()
        if self._channel is None:
            return
        self._state = self.PLAYING
        self._run_started = time.monotonic()
        self._base = 0.0
        self._ended = False

    def pause(self) -> None:
        if self._state != self.PLAYING or self._channel is None:
            return
        self._base = self.position()
        self._channel.pause()
        self._state = self.PAUSED

    def resume(self) -> None:
        if self._state != self.PAUSED or self._channel is None:
            return
        self._channel.unpause()
        self._state = self.PLAYING
        self._run_started = time.monotonic()

    def stop(self) -> None:
        if self._channel is not None:
            try:
                self._channel.stop()
            except Exception:
                pass
        self._channel = None
        self._state = self.IDLE
        self._base = 0.0

    # --------------------------------------------------------------- position
    def position(self) -> float:
        if self._sound is None:
            return 0.0
        if self._state == self.PLAYING:
            return min(self._base + (time.monotonic() - self._run_started), self._duration)
        if self._state == self.PAUSED:
            return self._base
        return 0.0

    def seek_to(self, seconds: float) -> None:
        """Restart playback (pygame has no reliable file-level seek to offset)."""
        was_active = self.is_active()
        self.stop()
        if was_active:
            self.play()

    # --------------------------------------------------------------- polling
    def pump(self) -> bool:
        """Advance playback state. Returns True the moment playback ends."""
        if self._state == self.PLAYING and self._channel is not None:
            if not self._channel.get_busy():
                self._state = self.IDLE
                self._base = self._duration
                if not self._ended:
                    self._ended = True
                    if self.on_end is not None:
                        self.on_end()
                    return True
        return False

    def close(self) -> None:
        try:
            self.stop()
            if self._pygame is not None:
                self._pygame.mixer.quit()
        except Exception:
            pass
