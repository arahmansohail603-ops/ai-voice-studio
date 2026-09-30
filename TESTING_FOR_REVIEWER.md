# Testing AI Voice Studio

A short guide for anyone who wants to try the app: what to run, what works
immediately, and what to click. [SETUP_FOR_REVIEWER.md](SETUP_FOR_REVIEWER.md)
has the full installation details and troubleshooting; this page is the
"just test it" version.

## 1. Message to forward

Copy the block below and send it as-is.

```
Assalam-o-Alaikum sir, AI Voice Studio test karne ke liye yeh karein.

1. Link kholein:
   https://github.com/arahmansohail603-ops/ai-voice-studio
   (Code -> Download ZIP)

2. ZARURI: ZIP ko OneDrive ya Desktop par mat kholein. Ek normal folder
   banayein, masalan  C:\Testing\AI-Voice-Studio  aur wahan unzip karein.

3. Us folder mein PowerShell kholein (folder par right-click ->
   "Open in Terminal") aur yeh do commands chalayein:

   powershell -ExecutionPolicy Bypass -File scripts\setup-dev.ps1
   powershell -ExecutionPolicy Bypass -File scripts\start-app.ps1

App khud khul jayega. Koi account, koi API key aur koi internet (setup ke
baad) nahi chahiye. Pehli baar 5-10 min lagenge; uske baad sab offline
chalta hai.

Agar app license key poochhe to setup script ne jo key print kiya tha woh
paste kar dein (wo LICENSE-KEY.txt file mein bhi likha hota hai).
```

Requirements: Windows 10/11 and Python 3.10, 3.11 or 3.12 on `PATH`
(<https://www.python.org/downloads/>, install karte waqt **Add python.exe to
PATH** zaroor tick karein). About 1 GB free disk space.

## 2. Why not OneDrive

The app writes its data, models and history to disk at runtime, and OneDrive
syncs and re-points that folder while it is being written to. The launcher
resolves the project path through that folder, which is a known source of
start-up failures. Keep the project in a plain local folder such as
`C:\Testing\AI-Voice-Studio`.

## 3. What works straight after setup

| Feature | Works? | Notes |
|---|---|---|
| Text to Speech | **Yes** | The default engine is Qwen3-TTS, whose model is a ~4.5 GB download. That model is **not** installed, so the app switches itself to the built-in Windows system voice and speaks through it. No large download, no error. |
| Voice Recorder | **Yes** | Needs a working microphone. Records, pauses, resumes, saves. |
| History | **Yes** | Every generated clip and note is listed with a timestamp and can be filtered. |
| Settings | **Yes** | Includes the dark/light appearance switch, output folder, sample rate. |
| Voices screen | **Yes** | Lists the offline system voices that are available on this machine. |
| Models screen | **Yes** | Shows the catalogue and explains what each engine needs before you download anything. |
| My Voice (voice cloning) | **No** | Opens with a "Voice Cloning Notice" banner, and Home and Settings both label it *heavy / experimental*. Needs an NVIDIA GPU and a ~4.5 GB model, and it is off by default. |
| Speech to Text | **No** | Needs the optional Vosk package **and** a per-language model. See section 5. |
| Translate to… | **No** | Needs the optional Argos package **and** a per-language-pair model. See section 5. |
| MP3 export | **Yes** | The bundled `imageio-ffmpeg` wheel ships its own ffmpeg, so no separate ffmpeg install is required. |

Two points that are easy to mistake for bugs:

- **There is no separate Translate screen.** "Translate to:" is a dropdown
  inside the Text to Speech and Speech to Text screens, set to
  *Off (no translation)* by default. Translation runs **before** speech, so
  it needs Argos.
- **Text to Speech falling back to a system voice is normal**, not a
  failure. It is what lets the app be useful before the 4.5 GB model exists.

## 4. UI test checklist

Work through the sidebar in this order. The order matters: Home has shortcuts
into the other screens.

| # | Screen | Try this | Expect |
|---|---|---|---|
| 1 | Home | Read the shortcuts, move between sections | Cards respond, layout does not jump or overlap |
| 2 | Text to Speech | Type a short English line, press the speak/convert button | A clip is produced and plays; a note about the system voice is acceptable |
| 3 | Text to Speech | Change speed and pitch, convert again | Rate changes audibly, no crash |
| 4 | Text to Speech | Change the language, convert | Either a matching system voice is chosen, or a clear "no voice installed" message |
| 5 | Voice Recorder | Record, pause, resume, stop, then save | Timer and input meter move while recording; the saved file plays back |
| 6 | Speech to Text | Open the screen and try to transcribe | A clear "vosk is not installed" style message (see section 3) |
| 7 | My Voice | Open the screen | The "Voice Cloning Notice" banner and its requirements are shown; nothing hangs |
| 8 | Models | Open the screen, look at an entry, close it | Sizes and requirements are readable; no download starts on its own |
| 9 | Voices | List, filter, search the system voices | List is populated and filtering works |
| 10 | History | Confirm the items from steps 2 and 5 are listed, filter by type, play one back | Timestamps are correct and playback works |
| 11 | Settings | Switch appearance dark -> light and back | Theme changes cleanly, no unreadable text |
| 12 | Settings | Close the app, start it again | Settings, history and window size are remembered; no license prompt loop |

## 5. Turning on Speech to Text and Translate (optional)

Both are held back from the base install on purpose: Whisper alone pulls in
torch (~2.5 GB), so the default install stays small. The app therefore shows a
"not installed" message instead of failing. Two steps are needed per feature —
the Python package, **then** the model.

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
```

That installs Vosk and Argos. The models still have to be downloaded, and the
app offers them on the **Models** screen: pick a Vosk model for the language
you want to transcribe, and an Argos package for the pair you want to
translate into. Each model downloads once and then works offline.

## 6. Reporting something that looks wrong

Please send three things; together they make almost any report actionable:

1. **What you did** and **what you expected** to happen.
2. **A screenshot** of the screen.
3. **The startup log**:

   ```
   %LOCALAPPDATA%\AI Voice Studio\startup.log
   ```

   Open it by pasting the path into Explorer. If the app refused to start at
   all, this log is the only place the real error appears — the window shows
   a generic message.

Generated audio, settings and history live in
`%USERPROFILE%\AI Voice Studio Data`. Clearing that folder resets the app to
its first-launch state; it is safe to delete, but it deletes the history.