# AI Voice Studio

A professional, advanced Python application that converts text into natural
speech, records voice notes, transcribes speech to text and translates between
languages — all running **on your own machine**.

The audio features are designed to run locally without cloud AI services or API keys.
The application is commercially licensed: activation and periodic lease validation
require a configured license server.

## Quick start

No account, cloud service or API key is required. From a fresh clone, in
PowerShell:

```powershell
# 1. one-time setup: virtualenvs, signing keys, local license server, your key
powershell -ExecutionPolicy Bypass -File scripts\setup-dev.ps1

# 2. start the app
powershell -ExecutionPolicy Bypass -File scripts\start-app.ps1
```

`setup-dev.ps1` creates both virtual environments, generates a signing key pair
for your machine, writes the local license server configuration, migrates its
database and issues a license key bound to your computer. No secret is ever
committed: the private key stays in `server\.env` and the issued key in
`LICENSE-KEY.txt`, both git-ignored.

See [SETUP_FOR_REVIEWER.md](SETUP_FOR_REVIEWER.md) for the full walkthrough,
optional offline engines and troubleshooting.

Sending the app to someone to try? [TESTING_FOR_REVIEWER.md](TESTING_FOR_REVIEWER.md)
has a ready-to-send message, a "what works immediately" table and a UI test
checklist.

## Features

| Feature | Details |
|---|---|
| Text to Speech | **Qwen3-TTS** local neural voices by default (fully offline; uses an NVIDIA GPU fast, falls back to the slow CPU if none). Falls back to offline **system voices** (`pyttsx3` / Windows SAPI5) when unavailable. Optional **Piper** offline neural voices. No cloud, no Microsoft services. |
| Voice Recorder | Record / pause / resume / stop with a live input meter and timer; save as WAV or MP3 voice notes. |
| Speech to Text | Live microphone transcription with the local **Vosk** engine (offline, no internet). Optional Whisper if installed. |
| Translate | Offline translation with **Argos Translate** (local language packages). |
| My Voice | Optional **experimental** voice cloning with Coqui XTTS-v2, or the higher-quality **Qwen3-TTS** Base engine (CUDA GPU recommended). Heavy (needs `torch` + ~8 GB RAM / ~4.5 GB download) and clearly labelled; falls back to a system voice. |
| History | Timestamped, filterable list of every generated speech, voice note and transcription. |
| Settings | Defaults, speech engine, devices, sample rate, output folder and data management. |

## Requirements

- Windows (any machine with Python **3.10 – 3.12**) — other OSes usually work too.
- A commercial PyQt5 license appropriate for your distribution, plus a reachable
  license server for activation and lease validation.
- **No internet required** for the core audio features after models are installed.
  Piper voice models, Vosk recognition models and Argos language packages may
  download once, then work offline.
- A microphone for recording/transcribing.
- **ffmpeg** *only* if you want MP3 export:
  `winget install ffmpeg` (or download from https://ffmpeg.org). Without it the
  app gracefully saves everything as WAV.

> Note: the app uses `sounddevice` for microphone capture, so **PyAudio is not
> required** even though SpeechRecognition is used for audio containers.

## Installation

```powershell
# 1. create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install runtime dependencies
pip install -r requirements.txt

# 3. OPTIONAL — fully-offline engines (Vosk STT, Argos translation, Piper)
pip install -r requirements-local.txt

# 4. OPTIONAL — local Qwen3-TTS neural voices + voice cloning (torch + model
#    ~4.5 GB). Uses an NVIDIA GPU automatically when available; otherwise runs
#    on the (slow) CPU. For GPU speed, install CUDA torch first, then clones.
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements-clone.txt
```

## Running

```powershell
python main.py
```

## Licensing configuration

Set these environment variables before launching a development build:

- `AI_VOICE_STUDIO_LICENSE_URL` — HTTPS URL of the licensing server.
- `AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS` — JSON object mapping server key IDs to
  Ed25519 public keys. The desktop app only receives public keys; signing keys stay
  on the server.
- `AI_VOICE_STUDIO_LICENSE_REFRESH_SECONDS` — optional refresh interval, minimum
  300 seconds.
- `AI_VOICE_STUDIO_DATA` — optional runtime-data root; defaults to
  `%USERPROFILE%\AI Voice Studio Data` on Windows.

The server's deployment variables are documented in `server/.env.example`. Never
put a signing private key or the server HMAC pepper in the desktop application.
The client keeps its license state encrypted with a machine-bound key and refreshes
the signed lease periodically.

## Building

Install the build dependency, set the public trust configuration, and create a
standalone executable with Nuitka:

```powershell
pip install -r requirements-build.txt
$env:AI_VOICE_STUDIO_LICENSE_URL = "https://license.example.com"
$env:AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS = '{"current-v1":"<Ed25519 public key>"}'
python build.py
python build.py --onefile
python build.py --include-optional
```

The build embeds the public trust configuration in the executable and removes
its temporary generated module afterward. Production builds should use HTTPS and
must not be run with placeholder keys.

`requirements-build.txt` installs the tested compiler range `Nuitka>=2.8.6,<3`;
2.8.6 is the first release whose package configuration resolves the
`win32ctypes` core backend.

Use `--include-optional` when the executable should bundle installed local AI,
speech-recognition, and translation packages. The build includes the `app` package
and `assets/` when present, but must not include `project_code.txt` or other
source/documentation files in a release.

### Keystore packaging

The local license store is encrypted with a key held in the operating-system
keystore, so the build includes `keyring`, every keyring backend, the
`keyring` distribution metadata that declares the `keyring.backends` entry
points, and on Windows the Credential Manager chain `keyring.backends.Windows` →
`win32ctypes` (plus the `cffi` variant when `cffi` is installed, because
`win32ctypes` selects its backend from that import) with the `pywin32` fallback.
Nuitka cannot see keyring's runtime backend discovery, so the build:

1. fails when the build environment has no `keyring` at all,
2. after a successful compile, stores, reads back and deletes one random test
   secret through `keyring` in the build environment, which fails the build when
   no backend is available (`RuntimeError: Requires Windows and pywin32` is the
   usual cause),
3. verifies that the standalone folder really contains `keyring`,
   `keyring.backends`, the platform backend and the `win32ctypes` core backend,
   and warns when the keyring metadata is absent.

```powershell
python build.py --check-only            # re-run the checks against an existing dist
python build.py --skip-keyring-check    # compile without the checks (not recommended)
```

The keystore check writes and removes a single
`AI Voice Studio build check <random>` credential; it never touches the real
license entry. `--onefile` skips step 3 because the standalone folder is packed
into the executable.

## One instance per data directory

The desktop app claims a local single-instance lock keyed to its data directory
before the licensing dialog opens. If another copy is already running against the
same `AI_VOICE_STUDIO_DATA` location, the new copy exits instead of competing for
the machine keyring and the encrypted license store.

## Offline engines

| Feature | Engine | Notes |
|---|---|---|
| Text to Speech | **Qwen3-TTS** (default) | GPU when available, else CPU (slow); ~4.5 GB model, installed from the Models screen |
| Text to Speech | System voices (`pyttsx3`) | Fallback / backup; nothing to download; uses OS voices |
| Text to Speech | **Piper** (optional) | Neural voices, installed from the Models screen |
| Speech to Text | **Vosk** | ~50 MB per language, installed from the Models screen |
| Voice cloning | **Coqui XTTS-v2** / **Qwen3-TTS** | Installed from the Models screen; heavy (~2 GB / ~4.5 GB) |
| Translate | **Argos Translate** | Language pairs install once (free), then offline |

Core audio features do not use paid or Microsoft/cloud AI services. The
application license is separate from the optional local model downloads.
`deep-translator` is not required and is not installed by default.

## Models

Every AI model is delivered from a **signed model catalog** and installed by
you on the **Models** screen. The app never downloads a model behind your back:
an engine that needs one is only selectable once its model is on disk, and
picking a gated engine tells you what to install instead of failing mid-task.

The catalog is a JSON document signed with an Ed25519 key, using the same
envelope format as the licensing system. A download is installed only after its
bytes match the SHA-256 in the signed catalog, so a tampered catalog or a
corrupted download is rejected rather than installed.

### Configuration

| Variable | Purpose |
|---|---|
| `AI_VOICE_STUDIO_MODEL_MANIFEST_URL` | HTTPS URL of the signed `catalog.json` |
| `AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS` | JSON `{"key-id": "<hex or base64 Ed25519 public key>"}` |
| `AI_VOICE_STUDIO_MODEL_MIRROR` | Optional base URL for an offline/corporate mirror |
| `AI_VOICE_STUDIO_MODEL_CATALOG_TIMEOUT` | Catalog fetch timeout, seconds (default 20) |
| `AI_VOICE_STUDIO_MODEL_DOWNLOAD_TIMEOUT` | Download socket timeout, seconds (default 60) |
| `AI_VOICE_STUDIO_MODEL_DISK_HEADROOM` | Free-space safety margin, bytes (default 512 MB) |

With no manifest URL configured the app falls back to the catalog bundled at
`app/resources/catalog.json`, which is schema-validated on every load.

Downloads are **resumable** (HTTP `Range`), run on a worker thread with a
cancel button, check free disk space before starting, and are installed
atomically — a partial download is never visible to an engine.

### Publishing a model

Models are hosted as GitHub Release assets. Because a single Release asset is
capped at 2 GB, larger models (Qwen3-TTS at ~4.5 GB) are published as an
ordered sequence of parts that the app joins and verifies as one archive.

```bash
# 1. Create the signing key once and print the public half to embed in the build
python tools/publish_model.py --key model-catalog.key --key-id model-2026 \
                              --print-public-key

# 2. Dry run: pack, chunk and compute digests without uploading
python tools/publish_model.py --repo owner/repo --tag models-2026-01 \
    --id vosk-small-en-us-0.15 --kind stt --name "Vosk English (US) small" \
    --version 0.15 --engine vosk --languages en-US \
    --install-dir vosk-models/small_en-us --marker am --marker graph \
    --source ./vosk-model-small-en-us-0.15 \
    --key model-catalog.key --key-id model-2026 --dry-run

# 3. Publish: uploads the assets, then re-signs tools/catalog.json
python tools/publish_model.py ...same flags without --dry-run
```

The tool needs the [GitHub CLI](https://cli.github.com) authenticated
(`gh auth login`); the private signing key never enters the application.

Models are installed under `AI_VOICE_STUDIO_DATA/models` (by default
`%USERPROFILE%\AI Voice Studio Data\models`).

## Windows microphone permissions

The app checks for an input device at startup. If you see "No microphone
found", open **Settings → Privacy & security → Microphone** and make sure
*"Let desktop apps access your microphone"* is **ON**, then restart the app.

## Project layout

```
app/
  config.py            paths, defaults, locale maps
  core/                engine layer (no UI)
    tts_engine.py      Qwen3-TTS (local) + system voices + optional Piper
    stt_engine.py      live offline speech recognition (sounddevice capture)
    vosk_engine.py     offline Vosk recognition over catalog-installed models
    model_catalog.py   signed model-catalog schema + Ed25519 verification
    model_manager.py   resumable download, verify, atomic install, removal
    recorder.py        record / pause / save, input-level meter
    player.py          pygame playback (play/pause/stop, position)
    voice_cloner.py    optional Coqui XTTS-v2 cloning
    qwen_cloner.py     optional Qwen3-TTS Base cloning (CUDA GPU)
    translator.py      offline Argos translation
    audio_utils.py     pydub conversions, ffmpeg detection, WAV writing
    async_runner.py    asyncio loop on a background thread
    errors.py          friendly exceptions + soft dependency guards
  gui/
    app.py             main window, sidebar nav, screen router
    widgets.py         player bar, badges, toasts, busy buttons, meters
    screens/           home, TTS, recorder, STT, my voice, models, history, settings
  licensing/           signed lease client, verification, protected state
  services/
    settings_service.py  JSON settings
    history_service.py   JSON history
    file_service.py      guaranteed folder layout + naming
  resources/
    catalog.json       bundled fallback model catalog (dev builds)
server/                Django licensing API (deployment-only)
tools/
  publish_model.py     pack, chunk, upload to a GitHub Release, sign catalog.json
AI Voice Studio Data/  runtime settings, history, models, and generated audio
  settings.json history.json models/ output/
  tts/ recordings/ transcripts/ voices/ clones/
```

## Voice cloning ethics notice

"Voice cloning" appears in the **My Voice** screen only in clearly labelled
contexts, and the app refuses to synthesise until you confirm that the voice
sample is one you own or have permission to use. Cloning is **off by default**
(heavy/experimental) and falls back to a system voice. Do not clone someone
else's voice without their permission.

## Troubleshooting

- **"Missing required library"** — the dialog shows the exact command; install
  it and restart.
- **No offline voices** — ensure `pyttsx3` is installed; voices come from your
  OS. Optionally add a Piper voice to `%USERPROFILE%\AI Voice Studio Data\models\piper-voices\`.
- **Recognition needs a model** — install `vosk` (see `requirements-local.txt`).
  The model downloads once per language on first use.
- **Translation unavailable** — install `argostranslate`; the first use of a
  language pair downloads its small model (one-time internet).
- **MP3 disabled** — install ffmpeg and restart.
- **Playback unavailable** — no audio output device detected; files are still
  written to disk and playable in any media player.
