# Running AI Voice Studio yourself

This guide takes a fresh clone to a running desktop app. No account, no cloud
service and no API key is required: the licensing server runs on your own
machine and signs a license that is bound to your computer.

## Requirements

- Windows 10 or 11 (other systems usually work, but this is what is tested)
- Python 3.10, 3.11 or 3.12 on your `PATH` — <https://www.python.org/downloads/>
  Tick **Add python.exe to PATH** during installation
- About 1 GB of free disk space and 10 minutes

## Quick start

From the repository root, in PowerShell:

```powershell
# 1. one-time setup: virtualenvs, signing keys, local license server, your key
powershell -ExecutionPolicy Bypass -File scripts\setup-dev.ps1

# 2. start the app (it launches the license server for you)
powershell -ExecutionPolicy Bypass -File scripts\start-app.ps1
```

The setup script prints a license key and also writes it to `LICENSE-KEY.txt`.
If the app asks for a key, paste that value.

The first run downloads a few packages. After that everything is offline.

## What the setup script does

| Step | Result |
|---|---|
| Creates `.venv` | installs the app dependencies |
| Creates `server\.venv` | installs the Django license server dependencies |
| Generates an Ed25519 key pair | a **new** signing key for this machine, every time |
| Writes `server\.env` | the private key, a Django secret and an HMAC pepper |
| Writes `scripts\license-dev.env` | the matching **public** key, trusted by the app |
| Runs `manage.py migrate` | creates `server\db.sqlite3` |
| Issues a license key | bound to this computer, written to `LICENSE-KEY.txt` |

The private key never leaves your machine. `server\.env`, `scripts\license-dev.env`
and `LICENSE-KEY.txt` are all git-ignored, so running the script can never
accidentally commit a secret.

Re-running the script is safe: it reuses the key pair already in `server\.env`
if one is present, so you keep the keys you have already activated.

## Optional offline engines

The base install gives you system voices, recording, playback and history. To add
the fully-offline AI engines, run this once inside the app's virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
```

That adds Vosk (offline speech-to-text), Argos Translate (offline translation) and
Whisper. Voice cloning additionally needs `requirements-clone.txt` and about 4.5 GB
of model weights.

## Where things are stored

| What | Where |
|---|---|
| License server database | `server\db.sqlite3` |
| Server secrets (private key) | `server\.env` |
| App trust config (public key) | `scripts\license-dev.env` |
| Your license key | `LICENSE-KEY.txt` |
| Encrypted license state | `%LOCALAPPDATA%\AI Voice Studio\license.dat` |
| Generated audio and history | `%USERPROFILE%\AI Voice Studio Data` |
| Startup log | `%LOCALAPPDATA%\AI Voice Studio\startup.log` |

## Running the tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The license server tests are a separate suite:

```powershell
cd server
.\.venv\Scripts\Activate.ps1
python manage.py test licenses
```

## Troubleshooting

**"Python 3.10, 3.11 or 3.12 is required but was not found"**
Install a supported version and tick *Add python.exe to PATH*, then open a new
PowerShell window.

**"execution of scripts is disabled on this system"**
Every command above already passes `-ExecutionPolicy Bypass`. If you see this
while running something else, use the same flag, or run:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**The app asks for a license key every time**
The app could not reach the license server on `http://127.0.0.1:8000`. Check
that `server\db.sqlite3` exists and that `server\.env` has a `LICENSE_SIGNING_KEYS`
line, then re-run `scripts\setup-dev.ps1`.

**`Licence response signature is invalid`**
`scripts\license-dev.env` and the private key in `server\.env` disagree. This
happens if the two files come from different setups. Delete `server\.env` and run
`scripts\setup-dev.ps1` again to regenerate a matching pair.

**`The local license belongs to another device`**
The encrypted license state was issued on a different machine. Delete
`%LOCALAPPDATA%\AI Voice Studio\license.dat` and paste your key again.

**The app does not start and no window appears**
Read `%LOCALAPPDATA%\AI Voice Studio\startup.log` — the launcher reports startup
failures there.

## A note on the licensing design

The activation flow is a real part of the project, not a stub, so it is worth
knowing what happens on startup:

1. The app derives a stable device id from the Windows machine GUID.
2. It asks the server to activate the key. The server records the activation
   against that device id and returns a **signed lease**.
3. The app verifies the Ed25519 signature, the device binding and the expiry
   before it opens the main window.
4. The lease is refreshed on a timer. If the server cannot be reached inside the
   grace period, the app stops.

Because the key you get is bound to one machine, a license issued by
`setup-dev.ps1` will not work on someone else's computer — that is the intended
behaviour, and it is why you generate your own key instead of using a shared one.
