# AI Voice Studio

A professional, advanced Python application that converts text into natural
human-like speech, records voice notes, and transcribes speech to text — all
wrapped in a modern dark-themed GUI.

## Features

| Feature | Details |
|---|---|
| Text to Speech | `edge-tts` neural voices (~35+ languages, many voices per language) with speed and pitch control. Automatic offline fallback to `pyttsx3`. |
| Voice Recorder | Record / pause / resume / stop with a live input meter and timer; save as WAV or MP3 voice notes. |
| Speech to Text | Live microphone transcription (Google engine; optional offline Whisper). Copy text or send it straight to TTS. |
| My Voice | Record or upload an authorised voice sample, create a voice profile, and generate speech resembling that voice with the local **Coqui XTTS-v2** model. Clearly labelled; falls back to a neural voice if the model is unavailable. |
| History | Timestamped, filterable list of every generated speech, voice note and transcription. |
| Settings | Defaults, devices, sample rate, output folder and data management. |

## Requirements

- Windows (any machine with Python **3.10 – 3.12**) — other OSes usually work too.
- Internet for `edge-tts` speech generation and Google speech recognition.
- A microphone for recording/transcribing.
- **ffmpeg** *only* if you want MP3 export:
  `winget install ffmpeg` (or download from https://ffmpeg.org). Without it the
  app gracefully saves everything as WAV.

> Note: the app uses `sounddevice` for microphone capture, so **PyAudio is not
> required** even though SpeechRecognition is used for recognition.

## Installation

```powershell
# 1. create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install core dependencies
pip install -r requirements.txt
#    (Python 3.13+ automatically pulls pygame-ce, which provides the same
#     `pygame` import name used by this app)

# 3. OPTIONAL — real voice cloning (large torch + model download ~2 GB)
pip install -r requirements-clone.txt

# 4. OPTIONAL — offline speech recognition (heavy)
pip install openai-whisper
```

## Running

```powershell
python main.py
```

## Windows microphone permissions

The app checks for an input device at startup. If you see "No microphone
found", open **Settings → Privacy & security → Microphone** and make sure
*"Let desktop apps access your microphone"* is **ON**, then restart the app.

## Project layout

```
app/
  config.py            paths, defaults, locale maps
  core/                engine layer (no UI)
    tts_engine.py      edge-tts + pyttsx3 fallback
    stt_engine.py      live speech recognition (sounddevice capture)
    recorder.py        record / pause / save, input-level meter
    player.py          pygame playback (play/pause/stop, position)
    voice_cloner.py    Coqui XTTS-v2 cloning + fallback
    audio_utils.py     pydub conversions, ffmpeg detection, WAV writing
    async_runner.py    asyncio loop on a background thread
    errors.py          friendly exceptions + soft dependency guards
  gui/
    app.py             main window, sidebar nav, screen router
    widgets.py         player bar, badges, toasts, busy buttons, meters
    screens/           home, TTS, recorder, STT, my voice, history, settings
  services/
    settings_service.py  JSON settings
    history_service.py   JSON history
    file_service.py      guaranteed folder layout + naming
data/                  settings.json, history.json (auto-created)
output/                generated audio (auto-created)
  tts/ recordings/ transcripts/ voices/ clones/
```

## Voice cloning ethics notice

"Voice cloning" appears in the **My Voice** screen only in clearly labelled
contexts, and the app refuses to synthesise until you confirm that the voice
sample is one you own or have permission to use. When cloning is not
available, generated audio is marked **Fallback Voice**. Do not clone someone
else's voice without their permission.

## Troubleshooting

- **"Missing required library"** — the dialog shows the exact command; install
  it and restart.
- **edge-tts generation failed** — network issue; the app falls back to the
  offline `pyttsx3` engine (fewer voices, no pitch).
- **MP3 disabled** — install ffmpeg and restart.
- **Playback unavailable** — no audio output device detected; files are still
  written to disk and playable in any media player.
- **Clone model download stalls** — it is ~2 GB; wait a few minutes or run the
  download once with a stable connection.