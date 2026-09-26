# AI Voice Studio — Tech Stack

Complete technical reference for **AI Voice Studio v1.0.0**, an offline-first
Python desktop application for text-to-speech, voice recording, speech-to-text,
translation and optional voice cloning.

The audio engines are offline-first and local. AI Voice Studio itself is
commercially licensed: activation and periodic signed-lease validation require
a configured license server. Core audio features do not require cloud AI API keys.

---

## 1. Overview

| Item | Value |
|---|---|
| Application | AI Voice Studio |
| Version | 1.0.0 (`app/config.py:20`) |
| Type | Desktop GUI application (single-process, multi-threaded) |
| Entry point | `main.py` → `python main.py` |
| Architecture | Layered: **core** (engines, no UI) → **services** (persistence) → **gui** (PyQt5) |
| Offline-first | TTS, STT and translation can run without internet after model setup |
| Licensing | Commercial desktop license; signed leases validated through Django REST API |
| Packaging | Nuitka standalone or onefile executable via `build.py` |

---

## 2. Language & Runtime

| Item | Detail |
|---|---|
| Language | Python |
| Supported versions | **3.10 – 3.12** (3.13+ supported with `pygame-ce`) |
| OS focus | Windows (also works on macOS/Linux) |
| Style | PEP 8, Ruff, `from __future__ import annotations` in every module |
| Concurrency | `threading`, `queue`, `asyncio`, `concurrent.futures.Future`, `subprocess` |
| Packaging | Nuitka standalone/onefile build; development also runs from source via `requirements*.txt` |

---

## 3. GUI Stack

| Component | File | Detail |
|---|---|---|
| GUI framework | — | **PyQt5** (commercial distribution license required) |
| Theme | `app/gui/theme.py` | Qt stylesheet, color palette, fonts, radii; dark appearance |
| Main window / router | `app/gui/app.py` | `QMainWindow`/`QStackedWidget`, sidebar navigation, shared state |
| Shared widgets | `app/gui/widgets.py` | `AudioPlayerBar`, `MicLevelMeter`, `BusyButton`, `MethodBadge`, `Screen`, toasts |
| Screens | `app/gui/screens/` | `home`, `text_to_speech`, `voice_recorder`, `speech_to_text`, `my_voice`, `history`, `settings` |
| Preflight | `main.py:38` | Checks PyQt5, cryptography and keyring before launch |

---

## 4. Core Engine Layer

All engines live in `app/core/` and are UI-free. Optional libraries load through
`app/core/errors.py` (`soft_import`, `module_available`, `require`) so missing
packages produce a friendly "pip install …" message instead of a crash.

### 4.1 Text-to-Speech (TTS) — `app/core/tts_engine.py`

Offline-first. Backend is chosen in Settings (`tts.backend`). **No Microsoft or
cloud services are used** (edge-tts has been removed).

| Backend | Library | Mode | Notes |
|---|---|---|---|
| Qwen3-TTS (**default**) | `qwen_tts` + `torch` | **Offline** | Uses an NVIDIA CUDA GPU automatically when present, else falls back to the (slow) CPU; model in HF hub cache `models/huggingface`; synthesises with a reference voice (auto-generated default or your recorded profile). |
| System | `pyttsx3` (Windows SAPI5 / macOS / espeak) | **Offline** | No download; voices come from the OS; fallback when Qwen is unavailable |
| Piper | `piper-tts` (optional) | **Offline** | Neural voices from `%USERPROFILE%\AI Voice Studio Data\models\piper-voices\*.onnx`; source-mode support only, not bundled by the default Nuitka release |

- Modes: `MODE_QWEN = "qwen3-tts"`, `MODE_SYSTEM`/`MODE_PYTTSSX3 = "pyttsx3"`,
  `MODE_PIPER = "piper"` (`app/core/tts_engine.py`).
- `available_backends()` returns `system` always, plus `piper`/`qwen3` when the
  package **and** a matching catalog-installed model are present (Qwen is active
  via `qwen_gpu_ready()`). `backend_status()` explains why one is unavailable.
- Qwen3-TTS auto-uses an NVIDIA CUDA GPU when available, otherwise the CPU
  (`QWEN_GPU_ONLY` in `app/config.py` can force strict GPU-only). It shares the same
  model instance as the My Voice clone engine (`QwenVoiceCloner`), so it loads once;
  failures fall back to the system voice.
- Output: WAV. Async work driven by `AsyncRunner` (asyncio loop on a background thread).

### 4.2 Speech-to-Text (STT) — `app/core/stt_engine.py`

| Backend | Library | Mode | Notes |
|---|---|---|---|
| Vosk (**default**) | `vosk` | **Offline** | Small per-language models installed from the signed catalog |
| Whisper | `openai-whisper` (optional) | **Offline** | Offered when installed |

- Microphone capture uses **`sounddevice`** on a dedicated capture thread.
- Recognition runs on a separate worker thread via a `queue.Queue`.
- Custom **VAD** (RMS thresholds, rolling noise floor, silence detection).
- No Google/cloud recognition path exists anymore.
- Vosk support: `app/core/vosk_engine.py`; per-language models are installed from
  the signed catalog into `%USERPROFILE%\AI Voice Studio Data\models\vosk-models\`.
  Vosk is listed only when a model for the selected language is present;
  `engine_status()` explains the gap and `ensure()` raises
  `ModelNotInstalledError` rather than downloading.

### 4.3 Model catalog & downloads — `app/core/model_catalog.py`, `app/core/model_manager.py`

| Concern | Implementation |
|---|---|
| Source of truth | `catalog.json` — a signed `ModelSpec` list, Ed25519, `canonical-json` envelope (same shape as the licensing lease) |
| Trust config | `MODEL_MANIFEST_URL` / `MODEL_PUBLIC_KEYS` / `MODEL_MIRROR_BASE` (`app/config.py`), all env-overridable via `AI_VOICE_STUDIO_MODEL_*` |
| Offline fallback | `app/resources/catalog.json` bundled by `build.py` (`--include-data-dir`), schema-validated on every load |
| Validation | Fails closed: unknown fields, non-HTTPS URLs, non-allow-listed hosts, bad digests, absolute/traversing `install_dir`, part gaps and duplicate ids are all rejected |
| Download | `urllib` with HTTP `Range` resume, 256 KiB chunks, per-part **and** whole-archive SHA-256, response capped at the declared size |
| Multi-GB models | Assets are ordered `part` chunks joined into one staged archive, so Qwen3-TTS (~4.5 GB) needs no double disk usage |
| Extraction | Staged outside `MODELS_DIR`, then `replace()`d into place — atomic, so a partial install is never loadable |
| Resource limits | Free-space precheck + headroom, `max_extracted_bytes` cap, `MAX_EXPANSION_RATIO` zip-bomb guard, unsafe-member skip |
| Concurrency | One download at a time (`_download_lock`); UI work on a `QThread` with signal-based progress and cancel |
| Publishing | `tools/publish_model.py` — deterministic zip, chunk, upload to a GitHub Release via `gh api`, upsert entry, re-sign catalog |

Catalog keys are a **separate trust domain** from licensing: rotating the model
key cannot affect license validation, and a compromised model key cannot mint
licenses. The private half never ships.

### 4.3 Voice Cloning — `app/core/voice_cloner.py`, `app/core/qwen_cloner.py` (optional, heavy)

| Item | Detail |
|---|---|
| Library (XTTS) | **Coqui TTS** (`TTS`) + **PyTorch** (`torch`) |
| Model (XTTS) | XTTS-v2 — `tts_models/multilingual/multi-dataset/xtts_v2` |
| Model size (XTTS) | ~2 GB, downloaded on first use |
| Library (Qwen) | **`qwen-tts`** package (`Qwen3TTSModel`) |
| Model (Qwen) | **Qwen3-TTS** Base — `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (voice clone from 3s reference + transcript) |
| Model size (Qwen) | ~4.5 GB, downloaded on first use |
| Qwen runtime | **CUDA GPU** recommended (`torch.bf16` + FlashAttention-2; `sdpa` fallback); CPU is very slow |
| Qwen languages | 10: Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian |
| Status | **Off by default** (`clone.enabled = False`); heavy/experimental |
| Engine select | `clone.engine` = `"xtts"` (default) or `"qwen"` (Settings → My Voice) |
| Fallback | A system (offline) voice, result clearly labelled |

Enable it in **Settings → My Voice** on a high-RAM machine.

### 4.4 Voice Recorder — `app/core/recorder.py`

| Item | Detail |
|---|---|
| Libraries | `sounddevice`, `numpy` |
| Features | Record / pause / resume / stop, live input-level meter, timer |
| Output | WAV natively; MP3 via ffmpeg |
| Defaults | 44100 Hz, mono |

### 4.5 Playback — `app/core/player.py`

| Item | Detail |
|---|---|
| Library | `pygame` (Python < 3.13) or `pygame-ce` (Python ≥ 3.13) |
| Features | Play / pause / stop, position polling |

### 4.6 Audio Utilities — `app/core/audio_utils.py`

| Item | Detail |
|---|---|
| Conversion | `pydub` (+ **ffmpeg**) for MP3 |
| WAV writing | `soundfile` |
| Arrays | `numpy` |
| ffmpeg discovery | `pydub.utils.which` → bundled `imageio-ffmpeg` → `shutil.which` |

Without ffmpeg the app still works and saves everything as WAV.

### 4.7 Translation — `app/core/translator.py`

| Item | Detail |
|---|---|
| Library | **Argos Translate** (`argostranslate`) |
| Mode | **Offline** (local language packages) |
| First use | The needed language pair downloads once (free), then offline |
| API | `Translator.supported_codes()`, `Translator().translate(text, target=...)` |

No Google/MyMemory cloud calls. `deep-translator` is no longer used.

### 4.8 Async Runner — `app/core/async_runner.py`

| Item | Detail |
|---|---|
| Purpose | Run asyncio coroutines without blocking the GUI |
| Design | Event loop on a background thread; returns a `Future` |

### 4.9 Errors — `app/core/errors.py`

| Type | Meaning |
|---|---|
| `AppError` | Base application error |
| `MissingDependencyError` | Optional library absent (includes pip hint) |
| `NetworkError` | One-time model download failed |
| `DeviceError` / `MicPermissionError` | Audio device / mic problem |
| `TTSGenerationError`, `TranslationError`, `CloneModelError` | Engine-specific failures |
| Helpers | `module_available`, `soft_import`, `require` |

---

## 5. Data & Persistence

| Component | File | Storage |
|---|---|---|
| Settings service | `app/services/settings_service.py` | `%USERPROFILE%\AI Voice Studio Data\settings.json` |
| History service | `app/services/history_service.py` | `%USERPROFILE%\AI Voice Studio Data\history.json` |
| File service | `app/services/file_service.py` | Folder layout + timestamped names |

```
%USERPROFILE%\AI Voice Studio Data\
  settings.json  history.json  output\
    tts/  recordings/  transcripts/  voices/  clones/
  models\  vosk-models\  piper-voices\  huggingface\
```

---

## 6. Architecture & Concurrency

**Layers**

- `app/config.py` — paths, defaults, language maps.
- `app/core/` — engines, no UI imports.
- `app/services/` — JSON persistence + file layout.
- `app/gui/` — PyQt5 screens and widgets.

**Threading model**

| Thread | Responsibility |
|---|---|
| GUI (main) | Qt event loop |
| asyncio loop | `AsyncRunner` background thread |
| Audio capture | `sounddevice` input stream |
| Recognition worker | Vosk / Whisper processing |
| Background loaders | XTTS model load, Vosk/Argos model download, Piper subprocess |

**Design principles**

- Offline by default; graceful degradation for every optional dependency.
- Friendly, actionable error dialogs instead of tracebacks.
- UI never blocks: heavy work runs on worker threads.

---

## 7. Dependency Matrix

### Required — `requirements.txt`

| Package | Version | Role |
|---|---|---|
| `PyQt5` | ≥ 5.15, < 5.16 | Commercial GUI framework |
| `cryptography` | ≥ 42 | Ed25519 signatures and AES-GCM license state |
| `keyring` | ≥ 25 | Machine-bound local license key storage |
| `pyttsx3` | ≥ 2.90 | Offline system-voice TTS |
| `SpeechRecognition` | ≥ 3.10.1 | Audio container + optional Whisper |
| `sounddevice` | ≥ 0.4.6 | Microphone capture (replaces PyAudio) |
| `soundfile` | ≥ 0.12.1 | WAV read/write |
| `pydub` | ≥ 0.25.1 | Audio format conversion |
| `numpy` | ≥ 1.26.0 | Audio arrays / VAD |
| `pygame` / `pygame-ce` | ≥ 2.5.2 | Playback |

### Offline engines — `requirements-local.txt`

| Package | Role | License |
|---|---|---|
| `vosk` | Offline speech-to-text | Apache-2.0 |
| `argostranslate` | Offline translation | MIT |
| `piper-tts` (optional) | Offline neural TTS | MIT |

### Optional cloning — `requirements-clone.txt`

| Package | Role |
|---|---|
| `TTS` (Coqui) | XTTS-v2 voice cloning |
| `qwen-tts` | Qwen3-TTS Base voice cloning (CUDA GPU recommended) |
| `torch` | Model runtime |
| `flash-attn` (optional) | FlashAttention-2 for fast Qwen3-TTS inference on NVIDIA GPUs |

## 8. Licensing and Protection

- `server/` is a deployment-only Django REST service; it stores HMAC-hashed license
  keys, binds activations to device identifiers, rate-limits requests, and signs
  short-lived Ed25519 leases.
- `app/licensing/` verifies signatures, validates lease time/state, and stores the
  local envelope using AES-GCM with a machine-bound keyring secret.
- `main.py` refreshes leases in a background `QThread` and blocks application
  access when activation is missing or validation cannot be restored.
- `build.py` uses Nuitka to produce a standalone/onefile executable. It includes
  the `app` package and `assets/` but never source dumps such as `project_code.txt`.
- `build.py` also includes `keyring`, every keyring backend, the `keyring`
  distribution metadata (the `keyring.backends` entry points keyring resolves at
  runtime) and, on Windows, the Credential Manager chain
  `keyring.backends.Windows` → `win32ctypes` with the `ctypes` or `cffi` core
  backend plus the `pywin32` fallback. Nuitka follows none of that dynamically, so
  the build ends with a keystore round-trip in the build environment and a check
  of the produced standalone folder (`--skip-keyring-check` opts out).
  `requirements-build.txt` pins Nuitka `>= 2.8.6`, the first release whose package
  configuration resolves the `win32ctypes` core backend.
- Release builds require `AI_VOICE_STUDIO_LICENSE_URL` and
  `AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS`; the latter must contain valid 32-byte
  Ed25519 public-key material. The values are embedded in a temporary module
  during compilation and that module is removed afterward.

---

### Not required (online extras)

| Package | Role |
|---|---|
| `deep-translator` | Optional online translation |
| `openai-whisper` | Optional offline STT |
| `imageio-ffmpeg` | Bundled ffmpeg for MP3 |
| `Pillow` (`PIL`) | Only for `tools/make_icon.py` |

---

## 9. Models & Downloads (free, one-time)

| Model | Source | When |
|---|---|---|
| Qwen3-TTS Base | HF hub cache `models/huggingface` (~4.5 GB) | Default TTS + My Voice engine `qwen` |
| Vosk small model | `https://alphacephei.com/vosk/models` | First use per language (~50 MB) |
| Argos language pair | Argos package index | First use per language pair |
| Piper voice | User-supplied `*.onnx` in `%USERPROFILE%\AI Voice Studio Data\models\piper-voices\` | Optional |
| XTTS-v2 | Coqui model hub (~2 GB) | Only if cloning engine is `xtts` |

After download, all of these work with no internet connection.

---

## 10. Third-party Tools

| Tool | Purpose | Install |
|---|---|---|
| **ffmpeg** | MP3 export/import | `winget install ffmpeg` (optional) |
| **Pillow** | Icon generation (`tools/make_icon.py`) | dev only |
| **Nuitka** | Standalone/onefile compiler; `>= 2.8.6, < 3` is the tested range | `pip install -r requirements-build.txt` |

---

## 11. Platform Requirements

| Requirement | Detail |
|---|---|
| Python | 3.10 – 3.12 recommended (3.13+ with `pygame-ce`) |
| Internet | Required for license activation/lease refresh; otherwise only for one-time model downloads |
| Microphone | Required for recording/transcription |
| ffmpeg | Optional; needed only for MP3 |
| RAM | ~8 GB required **only** if voice cloning is enabled |

---

## 12. Project Layout

```
main.py                     entry point + preflight
app/
  config.py                 paths, defaults, language maps
  core/                     engine layer (no UI)
    tts_engine.py           Qwen3-TTS local + system voices + optional Piper
    stt_engine.py           live offline transcription (Vosk)
    vosk_engine.py          offline model manager
    recorder.py             record / pause / save
    player.py               pygame playback
    voice_cloner.py         optional Coqui XTTS-v2 cloning
    qwen_cloner.py          optional Qwen3-TTS Base cloning (CUDA GPU)
    translator.py           offline Argos translation
    audio_utils.py          pydub / soundfile / ffmpeg
    async_runner.py         asyncio loop on a thread
    errors.py               friendly errors + soft imports
  gui/
    app.py, theme.py, widgets.py
    screens/                home, tts, recorder, stt, my voice, history, settings
  licensing/                signed lease client + protected local state
  services/
    settings_service.py     JSON settings
    history_service.py      JSON history
    file_service.py         folder layout + naming
server/                     Django licensing API (deployment-only)
AI Voice Studio Data/       runtime settings, history, models, output (outside repo)
tools/make_icon.py          app icon generator
```
