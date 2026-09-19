"""Text-to-speech engine.

Primary backend:  ``edge-tts`` (Microsoft neural voices — dozens of languages
and voices, supports speed *and* pitch). Requires internet.

Fallback backend: ``pyttsx3`` (fully offline, system voices, no pitch).
The engine automatically switches when edge-tts is missing or the network
request fails, and exposes a ``mode`` property so the UI can tell the user
exactly which backend produced the audio.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from typing import List, Optional

from app.core.async_runner import AsyncRunner
from app.core.errors import (
    MissingDependencyError,
    NetworkError,
    TTSGenerationError,
    module_available,
    soft_import,
)
from app.services import file_service


def _safe_attr(obj, *names, default=""):
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        else:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
    return default


class TTSEngine:
    """High-level TTS service with automatic edge-tts -> pyttsx3 fallback."""

    MODE_EDGE = "edge-tts"
    MODE_PYTTSSX3 = "pyttsx3"

    def __init__(self, runner: AsyncRunner, output_dir: Path):
        self.runner = runner
        self.output_dir = output_dir
        self._voices: List[dict] = []
        self._languages: List[str] = []
        self._mode = self.MODE_EDGE
        self._edge_available = module_available("edge_tts")
        if not self._edge_available:
            self._mode = self.MODE_PYTTSSX3
            self._load_local_voices()

    # ------------------------------------------------------------------ queue
    def refresh_voices_async(self, on_done) -> Future:
        """Reload voices off the GUI thread; calls ``on_done()`` when ready."""

        async def _fetch():
            if not self._edge_available:
                self._mode = self.MODE_PYTTSSX3
                self._load_local_voices()
                return
            try:
                import edge_tts

                raw = await edge_tts.list_voices()
                self._voices = [self._normalize_edge(v) for v in raw]
                self._mode = self.MODE_EDGE
            except Exception:
                # edge-tts installed but unreachable/errored -> offline fallback
                self._mode = self.MODE_PYTTSSX3
                if not self._voices:
                    self._load_local_voices()

        future = self.runner.run(_fetch())
        try:
            future.add_done_callback(lambda f: on_done())
        except Exception:
            pass
        return future

    # ---------------------------------------------------------------- voices
    @staticmethod
    def _normalize_edge(voice) -> dict:
        locale = str(_safe_attr(voice, "locale", "Locale"))
        short_name = str(_safe_attr(voice, "short_name", "ShortName"))
        friendly = str(_safe_attr(voice, "friendly_name", "FriendlyName", "name", "Name"))
        gender = str(_safe_attr(voice, "gender", "Gender", "Female"))
        return {
            "short_name": short_name,
            "friendly": friendly or short_name,
            "gender": gender,
            "locale": locale,
            "language": locale.split("-")[0].lower(),
            "source": "edge",
        }

    def _load_local_voices(self) -> None:
        pyttsx3 = soft_import("pyttsx3")
        self._voices = []
        try:
            engine = pyttsx3.init()
            for voice in engine.getProperty("voices"):
                vid = str(getattr(voice, "id", voice))
                name = str(getattr(voice, "name", "System voice"))
                langs = list(getattr(voice, "languages", []) or [])
                locale = ""
                if langs:
                    raw = langs[0]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    raw = str(raw).replace("_", "-").split(".")[0]
                    locale = raw
                if not locale:
                    locale = "en-US"
                self._voices.append(
                    {
                        "short_name": vid,
                        "friendly": name,
                        "gender": str(getattr(voice, "gender", "") or ""),
                        "locale": locale,
                        "language": locale.split("-")[0].lower(),
                        "source": "pyttsx3",
                    }
                )
        except Exception:
            self._voices = []

    def voices(self, language: Optional[str] = None) -> List[dict]:
        if not language:
            return list(self._voices)
        return [v for v in self._voices if v["language"] == language.lower()]

    def languages(self) -> List[str]:
        """Unique language codes found among loaded voices, first-seen order."""
        seen = []
        for v in self._voices:
            if v["language"] and v["language"] not in seen:
                seen.append(v["language"])
        return seen

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def voices_loaded(self) -> bool:
        return bool(self._voices)

    def find_voice(self, short_name: str) -> Optional[dict]:
        for v in self._voices:
            if v["short_name"] == short_name:
                return v
        return None

    # ------------------------------------------------------------- synthesis
    @staticmethod
    def rate_string(speed: float) -> str:
        percent = int(round((speed - 1.0) * 100))
        percent = max(-90, min(200, percent))
        return f"{percent:+d}%"

    @staticmethod
    def pitch_string(pitch: int) -> str:
        pitch = max(-50, min(50, int(pitch)))
        return f"{pitch:+d}Hz"

    def _default_path(self, ext: str = "mp3") -> Path:
        return file_service.unique_path(
            self.output_dir, file_service.timestamp_stem("tts"), ext
        )

    async def synthesize(
        self,
        text: str,
        voice_short_name: str,
        speed: float = 1.0,
        pitch: int = 0,
        output_path: Optional[str | Path] = None,
    ) -> dict:
        """Synthesize speech. Returns metadata dict {file, mode, voice, ...}."""
        text = (text or "").strip()
        if not text:
            raise TTSGenerationError("Please enter some text to convert.")

        voice = self.find_voice(voice_short_name) or {}
        source = voice.get("source", "edge")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if source == "pyttsx3":
            self._mode = self.MODE_PYTTSSX3
            path = self._pyttsx3_synth(text, voice_short_name, speed, output_path)
            return {"file": str(path), "mode": self.MODE_PYTTSSX3, "voice": voice_short_name}

        if not self._edge_available:
            raise MissingDependencyError(
                "edge_tts", "edge-tts",
                detail="Try again later or install edge-tts for neural voices.",
            )

        try:
            import edge_tts

            rate = self.rate_string(speed)
            pitch_str = self.pitch_string(pitch)
            path = Path(output_path) if output_path else self._default_path("mp3")
            path.parent.mkdir(parents=True, exist_ok=True)
            comm = edge_tts.Communicate(text, voice_short_name, rate=rate, pitch=pitch_str)
            await comm.save(str(path))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise NetworkError(
                "Speech generation failed (edge-tts network error). Check your "
                f"internet connection. ({exc})"
            ) from exc

        if not Path(path).exists() or Path(path).stat().st_size == 0:
            raise TTSGenerationError("The speech engine produced no audio output.")
        self._mode = self.MODE_EDGE
        return {"file": str(path), "mode": self.MODE_EDGE, "voice": voice_short_name}

    def _pyttsx3_synth(
        self,
        text: str,
        voice_id: str,
        speed: float,
        output_path: Optional[str | Path] = None,
    ) -> Path:
        pyttsx3_module = soft_import("pyttsx3")
        if pyttsx3_module is None:
            raise MissingDependencyError("pyttsx3", "pyttsx3")
        path = Path(output_path) if output_path else self._default_path("wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            engine = pyttsx3_module.init()
            engine.setProperty("rate", max(80, int(170 * speed)))
            engine.setProperty("volume", 1.0)
            try:
                engine.setProperty("voice", voice_id)
            except Exception:
                pass
            engine.save_to_file(text, str(path))
            engine.runAndWait()
            engine.stop()
        except Exception as exc:
            raise TTSGenerationError(f"Offline speech engine failed: {exc}") from exc
        if not path.exists() or path.stat().st_size == 0:
            raise TTSGenerationError("Offline speech engine produced no output.")
        return path

    # ------------------------------------------------------------- scheduling
    def synthesize_async(
        self,
        text: str,
        voice_short_name: str,
        speed: float = 1.0,
        pitch: int = 0,
        output_path: Optional[str | Path] = None,
    ) -> Future:
        coro = self.synthesize(text, voice_short_name, speed, pitch, output_path)
        return self.runner.run(coro)

    def prefill_default_voice(self) -> str:
        """A sensible default voice for the current default language."""
        language = "en"
        for v in self._voices:
            if v["language"] == language and v["gender"].lower() in ("female",):
                return v["short_name"]
        for v in self._voices:
            if v["language"] == language:
                return v["short_name"]
        if self._voices:
            return self._voices[0]["short_name"]
        return "en-US-AriaNeural"
