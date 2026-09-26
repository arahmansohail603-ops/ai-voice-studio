"""Text-to-speech engine — fully offline, no Microsoft services.

Primary backend: **Qwen3-TTS Base** (local neural voices, offline). The model
lives in the app's ``models/huggingface`` hub cache; synthesis uses a reference
voice (a neutral sample generated once from the offline system voice, or your
own recorded My Voice profile).

Backup backends:
- **system voices via ``pyttsx3``** (Windows SAPI5 / macOS ``say`` /
  Linux espeak). No download, no internet, no account.
- **Piper** neural voices (offline). Used only when the ``piper`` package is
  installed *and* a voice model exists in ``models/piper-voices/``.

The public API is unchanged: ``voices()``, ``languages()``, ``mode``,
``refresh_voices_async()`` and ``synthesize_async()``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import subprocess
import sys
from concurrent.futures import Future
from pathlib import Path

from app.config import PIPER_VOICE_DIR, QWEN_DEFAULT_REF_VOICE, QWEN_GPU_ONLY
from app.core.async_runner import AsyncRunner
from app.core.errors import (
    AppError,
    MissingDependencyError,
    TTSGenerationError,
    module_installed,
    soft_import,
)
from app.core.qwen_cloner import QWEN_LANGUAGE_NAMES, QwenVoiceCloner
from app.core.voice_cloner import CloneState
from app.services import file_service


def _last_lines(raw, count: int = 2) -> str:
    """The final ``count`` meaningful lines of subprocess output.

    A traceback's cause is at the bottom; the top is just frames. Truncating
    from the front is what hid the real reason Piper synthesis failed.
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
    return " ".join(lines[-count:])[:200]


class TTSEngine:
    """Offline neural TTS (Qwen3-TTS) with system and Piper backups."""

    MODE_SYSTEM = "pyttsx3"
    MODE_PYTTSSX3 = MODE_SYSTEM  # backwards-compatible alias
    MODE_PIPER = "piper"
    MODE_QWEN = "qwen3-tts"

    def __init__(
        self,
        runner: AsyncRunner,
        output_dir: Path,
        backend: str = "system",
        qwen=None,
        settings=None,
    ):
        self.runner = runner
        self.output_dir = output_dir
        self.backend = (backend or "system").strip().lower()
        self._voices: list[dict] = []
        self._languages: list[str] = []
        self._mode = self.MODE_PYTTSSX3
        self._qwen = qwen  # optional shared QwenVoiceCloner (model loaded once)
        self._settings = settings  # optional SettingsService for saved references
        self._qwen_available = module_installed("qwen_tts")
        self._piper_available = module_installed("piper")
        self._load_local_voices()
        self._load_qwen_voices()
        self._set_mode_from_backend()

    # ------------------------------------------------------------- backends
    @classmethod
    def available_backends(cls, manager=None) -> list[str]:
        """Backends usable on this machine (offline first).

        ``system`` needs nothing. ``piper`` and ``qwen3`` additionally need their
        model to be installed through the Models screen, so they are only listed
        when at least one matching model is present on disk.
        """
        backends = ["system"]
        if module_installed("piper") and cls._has_model(manager, kind="tts", engine="piper"):
            backends.append("piper")
        if module_installed("qwen_tts") and cls._has_model(manager, kind="tts", engine="qwen3"):
            backends.append("qwen3")
        return backends

    @staticmethod
    def _has_model(manager, *, kind: str, engine: str) -> bool:
        from app.core.model_manager import get_manager

        try:
            return (manager or get_manager()).find_installed(
                kind=kind, engine=engine
            ) is not None
        except AppError:
            return False

    def backend_status(self, backend: str, manager=None) -> str:
        """Explain why a backend is or is not selectable."""
        if backend == "system":
            return "Ready — uses the Windows voices already installed."
        if backend == "piper":
            if not module_installed("piper"):
                return "The 'piper' package is not installed."
            if not self._has_model(manager, kind="tts", engine="piper"):
                return "No Piper voice installed — add one from the Models screen."
            return "Ready"
        if backend == "qwen3":
            if not module_installed("qwen_tts"):
                return "The 'qwen_tts' package is not installed."
            if not self._has_model(manager, kind="tts", engine="qwen3"):
                return "Qwen3-TTS is not installed — download it from the Models screen."
            if not self.qwen_gpu_present:
                return (
                    "No NVIDIA GPU found — Qwen3-TTS will run on the CPU. "
                    "It is a 1.7B model, so expect it to take minutes per "
                    "clip instead of seconds, and the Voices screen will not "
                    "list its languages. Use Piper for fast offline speech."
                )
            if QWEN_GPU_ONLY and not self.qwen_gpu_present:
                return "Qwen3-TTS needs an NVIDIA GPU (GPU-only mode is on)."
            return "Ready — using the NVIDIA GPU."
        return f"Unknown text-to-speech backend '{backend}'."

    def set_backend(self, backend: str) -> None:
        self.backend = (backend or "system").strip().lower()
        self._set_mode_from_backend()

    def _set_mode_from_backend(self) -> None:
        if self.backend == "piper" and self._piper_available:
            self._mode = self.MODE_PIPER
        elif self.backend == "qwen3" and self.qwen_gpu_ready():
            self._mode = self.MODE_QWEN
        else:
            self._mode = self.MODE_PYTTSSX3

    def qwen_gpu_ready(self) -> bool:
        """True when Qwen3-TTS can actually run here.

        It needs the ``qwen_tts`` package and a loaded model. An NVIDIA CUDA
        GPU is used when present; without one it still runs, but on the CPU.
        """
        if not (self._qwen_available and self._qwen is not None):
            return False
        if QWEN_GPU_ONLY:
            return QwenVoiceCloner._cuda_available()
        return True

    @property
    def qwen_gpu_present(self) -> bool:
        """True when an NVIDIA CUDA GPU is available for Qwen3-TTS."""
        if not (self._qwen_available and self._qwen is not None):
            return False
        return QwenVoiceCloner._cuda_available()

    # ------------------------------------------------------------------ queue
    def refresh_voices_async(self, on_done) -> Future:
        """(Re)load voices off the GUI thread; calls ``on_done()`` when ready."""

        async def _fetch():
            self._load_local_voices()
            self._load_qwen_voices()
            self._set_mode_from_backend()

        future = self.runner.run(_fetch())
        try:
            future.add_done_callback(lambda f: on_done())
        except Exception:
            pass
        return future

    # ---------------------------------------------------------------- voices
    def _collect_local_voices(self) -> list[dict]:
        """System (pyttsx3) voices without touching ``self._voices``."""
        pyttsx3 = soft_import("pyttsx3")
        result = []
        if pyttsx3 is None:
            return result
        try:
            engine = pyttsx3.init()
            try:
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
                    result.append(
                        {
                            "short_name": vid,
                            "friendly": name,
                            "gender": str(getattr(voice, "gender", "") or ""),
                            "locale": locale,
                            "language": locale.split("-")[0].lower(),
                            "source": "pyttsx3",
                        }
                    )
            finally:
                engine.stop()
        except Exception:
            return []
        return result

    def _collect_piper_voices(self) -> list[dict]:
        """Piper voices found on disk (``models/piper-voices/*.onnx``)."""
        result: list[dict] = []
        if not PIPER_VOICE_DIR.is_dir():
            return result
        for model in sorted(PIPER_VOICE_DIR.glob("*.onnx")):
            name = model.stem
            config = model.with_suffix(".onnx.json")
            language = "en"
            if not config.exists():
                config = model.parent / f"{name}.onnx.json"
            try:
                import json

                if config.exists():
                    with open(config, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                    language = str(meta.get("language", {}).get("code", "en"))
                    language = language.split("-")[0].split("_")[0].lower() or "en"
            except Exception:
                pass
            result.append(
                {
                    "short_name": name,
                    "friendly": name.replace("_", " ").title(),
                    "gender": "",
                    "locale": language,
                    "language": language,
                    "source": "piper",
                    "path": str(model),
                }
            )
        return result

    def _load_local_voices(self) -> None:
        voices = self._collect_local_voices()
        # A Piper voice on disk is useless without the package to run it, so it
        # must not reach a dropdown where picking it guarantees a failure.
        if self._piper_available:
            voices.extend(self._collect_piper_voices())
        self._voices = voices

    def _load_qwen_voices(self) -> None:
        """Static tokens, one per Qwen3-TTS supported language (GPU only).

        Skipped entirely without a CUDA GPU. Qwen3-TTS 1.7B on a CPU is minutes
        per clip, so advertising its language list on a machine that has no
        NVIDIA card only produces voices the user cannot realistically use.
        """
        if not self.qwen_gpu_present:
            return
        if not self.qwen_gpu_ready():
            return
        seen = {v.get("short_name") for v in self._voices}
        for code, name in QWEN_LANGUAGE_NAMES.items():
            short = f"qwen-{code}"
            if short in seen:
                continue
            self._voices.append(
                {
                    "short_name": short,
                    "friendly": f"Qwen3-TTS ({name})",
                    "gender": "",
                    "locale": code,
                    "language": code,
                    "source": "qwen",
                }
            )

    def _viewable_voices(self) -> list[dict]:
        """Voices selectable for the active engine. Qwen voices are hidden
        unless the Qwen3-TTS engine is selected, so a stale selection can
        never trigger a (very slow) CPU run."""
        if self.backend == "qwen3":
            return list(self._voices)
        return [v for v in self._voices if v.get("source") != "qwen"]

    def voices(self, language: str | None = None) -> list[dict]:
        pool = self._viewable_voices()
        if not language:
            return pool
        return [v for v in pool if v["language"] == language.lower()]

    def languages(self) -> list[str]:
        """Unique language codes found among viewable voices, first-seen order."""
        seen = []
        for v in self._viewable_voices():
            if v["language"] and v["language"] not in seen:
                seen.append(v["language"])
        return seen

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def voices_loaded(self) -> bool:
        return bool(self._voices)

    def find_voice(self, short_name: str) -> dict | None:
        for v in self._voices:
            if v["short_name"] == short_name:
                return v
        return None

    def voice_usable(self, short_name: str) -> bool:
        """True when ``short_name`` is a voice this machine can actually speak.

        Guards saved settings: a name that is no longer offered (its engine was
        uninstalled, or the model was deleted) must be re-resolved rather than
        handed to the synthesiser, which would fail mid-generation.
        """
        voice = self.find_voice(short_name)
        if voice is None:
            return False
        source = voice.get("source")
        if source == "piper":
            return self._piper_available
        if source == "qwen":
            return self.qwen_gpu_ready()
        return True

    def _system_fallback_voice(self, language: str = "") -> str | None:
        """Best system (pyttsx3) voice in ``language``, or any offline voice."""
        if not any(v.get("source") == "pyttsx3" for v in self._voices):
            local = self._collect_local_voices()
            if local:
                seen = {v.get("short_name") for v in self._voices}
                self._voices.extend(v for v in local if v.get("short_name") not in seen)
        for v in self.voices(language) or list(self._voices):
            if v.get("source") == "pyttsx3":
                return v.get("short_name") or v.get("id")
        for v in self._voices:
            if v.get("source") == "pyttsx3":
                return v.get("short_name")
        return None

    # ------------------------------------------------------------- synthesis
    def _default_path(self, ext: str = "wav") -> Path:
        return file_service.unique_path(
            self.output_dir, file_service.timestamp_stem("tts"), ext
        )

    async def synthesize(
        self,
        text: str,
        voice_short_name: str,
        speed: float = 1.0,
        pitch: int = 0,
        output_path: str | Path | None = None,
    ) -> dict:
        """Synthesize speech. Returns metadata dict {file, mode, voice, ...}.

        All backends are offline. A ``source == "qwen"`` voice uses the local
        Qwen3-TTS model; any failure falls back to the offline system voice.
        """
        text = (text or "").strip()
        if not text:
            raise TTSGenerationError("Please enter some text to convert.")

        voice = self.find_voice(voice_short_name) or {}
        source = voice.get("source") or self._default_source()
        if source == "qwen" and self.backend != "qwen3":
            # A stale qwen-* voice must never launch the slow model when the
            # selected engine is not Qwen3-TTS — use an offline system voice.
            voice = {}
            source = "pyttsx3"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if source == "piper":
            self._mode = self.MODE_PIPER
            path = self._piper_synth(text, voice, speed, output_path)
            return {"file": str(path), "mode": self.MODE_PIPER, "voice": voice_short_name}

        if source == "qwen":
            return await self._qwen_synthesize(
                text, voice, speed, output_path
            )

        self._mode = self.MODE_PYTTSSX3
        used = voice_short_name
        if not voice:
            # The requested voice has no entry (e.g. a qwen-* name on a
            # GPU-less machine) — resolve it to a real offline system voice.
            lang = voice_short_name.split("-", 1)[1] if voice_short_name.startswith("qwen-") else ""
            used = self._system_fallback_voice(lang or "en") or voice_short_name
        path = self._pyttsx3_synth(text, used, speed, output_path)
        return {"file": str(path), "mode": self.MODE_PYTTSSX3, "voice": used}

    def _default_source(self) -> str:
        if self.backend == "piper" and self._piper_available:
            return "piper"
        if self.backend == "qwen3" and self.qwen_gpu_ready():
            return "qwen"
        return "pyttsx3"

    # ------------------------------------------------------------ qwen synthesis
    async def _qwen_synthesize(
        self,
        text: str,
        voice: dict,
        speed: float,
        output_path: str | Path | None,
    ) -> dict:
        """Offline Qwen3-TTS synthesis via the shared voice-clone model."""
        language = voice.get("language") or "en"
        voice_short_name = voice.get("short_name") or f"qwen-{language}"

        if not self.qwen_gpu_ready():
            return await self._qwen_fallback(
                text, language, speed, output_path,
                "Qwen3-TTS is not installed or unavailable on this device.",
            )

        reference = await asyncio.to_thread(self._ensure_qwen_reference)
        if reference is None:
            return await self._qwen_fallback(
                text, language, speed, output_path,
                "Could not prepare a Qwen3-TTS reference voice.",
            )

        state = await self._qwen_wait_ready(timeout=1200.0)
        if state != CloneState.READY:
            reason = getattr(self._qwen, "error", "") or "the Qwen3-TTS model could not load."
            return await self._qwen_fallback(text, language, speed, output_path, reason)

        path = Path(output_path) if output_path else self._default_path("wav")
        path.parent.mkdir(parents=True, exist_ok=True)

        pending: concurrent.futures.Future = concurrent.futures.Future()

        def _on_done(done_path: Path | None, exc: Exception | None) -> None:
            if exc is not None:
                if not pending.done():
                    pending.set_exception(exc)
            else:
                if not pending.done():
                    pending.set_result(done_path)

        self._qwen.synthesize(
            text=text,
            reference_wav=str(reference),
            language=language or "en",
            output_path=path,
            transcript="",
            on_change=None,
            on_done=_on_done,
        )
        try:
            await asyncio.wrap_future(pending)
        except Exception as exc:  # noqa: BLE001 - fall back to system voice
            return await self._qwen_fallback(
                text, language, speed, output_path, str(exc)[:120],
            )
        if not path.exists() or path.stat().st_size == 0:
            return await self._qwen_fallback(
                text, language, speed, output_path, "Qwen3-TTS produced no audio.",
            )

        self._mode = self.MODE_QWEN
        return {
            "file": str(path),
            "mode": self.MODE_QWEN,
            "voice": voice_short_name,
            "language": language,
        }

    async def _qwen_wait_ready(self, timeout: float = 1200.0) -> CloneState:
        """Wait for the shared Qwen model to load; returns its final state."""
        cloner = self._qwen
        if cloner is None:
            return CloneState.ERROR
        if cloner.state == CloneState.NOT_LOADED:
            cloner.load_background(on_change=lambda _s, _m: None)
        waited = 0.0
        while cloner.state == CloneState.LOADING and waited < timeout:
            await asyncio.sleep(0.5)
            waited += 0.5
        return cloner.state

    def _saved_qwen_reference(self) -> str:
        if self._settings is None:
            return ""
        try:
            return str(self._settings.get("tts", "qwen_reference", "") or "")
        except Exception:
            return ""

    def _ensure_qwen_reference(self) -> Path | None:
        """Reference voice for Qwen3-TTS: a saved profile, else a generated default."""
        saved = self._saved_qwen_reference().strip('"')
        if saved and Path(saved).exists():
            return Path(saved)
        if QWEN_DEFAULT_REF_VOICE.exists() and QWEN_DEFAULT_REF_VOICE.stat().st_size > 0:
            return QWEN_DEFAULT_REF_VOICE
        try:
            QWEN_DEFAULT_REF_VOICE.parent.mkdir(parents=True, exist_ok=True)
            voice = self._system_fallback_voice("en") or ""
            self._pyttsx3_synth(
                "This is the default voice for Qwen text to speech.",
                voice,
                1.0,
                QWEN_DEFAULT_REF_VOICE,
            )
        except Exception:
            return None
        if QWEN_DEFAULT_REF_VOICE.exists() and QWEN_DEFAULT_REF_VOICE.stat().st_size > 0:
            return QWEN_DEFAULT_REF_VOICE
        return None

    async def _qwen_fallback(
        self,
        text: str,
        language: str,
        speed: float,
        output_path: str | Path | None,
        reason: str,
    ) -> dict:
        """Best-effort offline fallback when Qwen3-TTS cannot be used."""
        fallback = self._system_fallback_voice(language or "")
        if not fallback:
            raise TTSGenerationError(
                f"Qwen3-TTS unavailable ({reason}) and no offline system voice found."
            )
        path = await asyncio.to_thread(
            self._pyttsx3_synth, text, fallback, speed, output_path
        )
        return {
            "file": str(path),
            "mode": self.MODE_PYTTSSX3,
            "voice": fallback,
            "fallback": True,
            "fallback_reason": reason,
            "voice_changed": True,
            "voice_changed_from": f"qwen-{language or 'en'}",
        }

    def _piper_synth(
        self,
        text: str,
        voice: dict,
        speed: float,
        output_path: str | Path | None = None,
    ) -> Path:
        """Offline Piper synthesis via the bundled ``python -m piper`` CLI."""
        if not self._piper_available:
            raise MissingDependencyError(
                "piper",
                "piper-tts",
                detail=(
                    f"The offline voice '{voice.get('short_name', 'this voice')}' "
                    "needs the Piper package, which is not installed. Install it, "
                    "or pick a different voice."
                ),
            )
        model = voice.get("path")
        if not model or not Path(model).exists():
            raise TTSGenerationError(
                "No Piper voice model found. Add a *.onnx voice to "
                f"'{PIPER_VOICE_DIR}' and refresh."
            )
        path = Path(output_path) if output_path else self._default_path("wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        length_scale = 1.0
        if speed and speed > 0:
            length_scale = max(0.5, min(2.0, 1.0 / float(speed)))
        cmd = [
            sys.executable, "-m", "piper",
            "--model", str(model),
            "--output_file", str(path),
            "--length_scale", f"{length_scale:.3f}",
        ]
        try:
            proc = subprocess.run(
                cmd,
                # Piper decodes stdin as UTF-8, so the text has to be encoded as
                # UTF-8 here too. Passing a str with text=True would encode it
                # with the Windows locale (cp1252), which mangles every
                # non-ASCII script to nothing and leaves Piper synthesising
                # zero audio -- surfacing as a baffling
                # "wave.Error: # channels not specified".
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=300,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise MissingDependencyError("piper", "piper-tts") from exc
        except subprocess.TimeoutExpired as exc:
            raise TTSGenerationError("Offline Piper synthesis timed out.") from exc
        if proc.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            # A failed run leaves a zero-byte wav behind; the history screen
            # would then list a silent file as a completed conversion.
            try:
                if path.exists() and path.stat().st_size == 0:
                    path.unlink()
            except OSError:
                pass
            # The tail of the traceback carries the cause; the head is just
            # boilerplate frames, so truncating from the front hid the reason.
            detail = _last_lines(proc.stderr or proc.stdout, 2)
            raise TTSGenerationError(f"Offline Piper synthesis failed. {detail}")
        return path

    def _pyttsx3_synth(
        self,
        text: str,
        voice_id: str,
        speed: float,
        output_path: str | Path | None = None,
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
            if voice_id:
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
        output_path: str | Path | None = None,
    ) -> Future:
        coro = self.synthesize(text, voice_short_name, speed, pitch, output_path)
        return self.runner.run(coro)

    def prefill_default_voice(self) -> str:
        """A sensible default voice for the current default language."""
        language = "en"
        # Prefer the configured backend's voices first.
        preferred = {
            "piper": "piper",
            "qwen3": "qwen",
        }.get(self.backend)
        pool = self._viewable_voices()
        if preferred:
            owned = [v for v in pool if v.get("source") == preferred]
            if owned:
                pool = owned + [v for v in pool if v.get("source") != preferred]
        for v in pool:
            if v["language"] == language and v["gender"].lower() in ("female",):
                return v["short_name"]
        for v in pool:
            if v["language"] == language:
                return v["short_name"]
        if pool:
            return pool[0]["short_name"]
        return ""
