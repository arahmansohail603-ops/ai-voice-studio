"""Application-wide configuration, default paths and constants."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from ._build_license_config import BUILD_LICENSE_CONFIGURED
    from ._build_license_config import (
        LICENSE_PUBLIC_KEYS as EMBEDDED_LICENSE_PUBLIC_KEYS,
    )
    from ._build_license_config import (
        LICENSE_SERVER_URL as EMBEDDED_LICENSE_SERVER_URL,
    )
except ImportError:
    BUILD_LICENSE_CONFIGURED = False
    EMBEDDED_LICENSE_SERVER_URL = None
    EMBEDDED_LICENSE_PUBLIC_KEYS = None

if BUILD_LICENSE_CONFIGURED and (
    not EMBEDDED_LICENSE_SERVER_URL or not EMBEDDED_LICENSE_PUBLIC_KEYS
):
    raise ValueError("The build-time license trust configuration is incomplete")

APP_NAME = "AI Voice Studio"
APP_VERSION = "1.0.0"

LICENSE_SERVER_URL = (
    (
        EMBEDDED_LICENSE_SERVER_URL
        if EMBEDDED_LICENSE_SERVER_URL is not None
        else os.environ.get("AI_VOICE_STUDIO_LICENSE_URL", "")
    )
    .strip()
    .rstrip("/")
)
LICENSE_PUBLIC_KEYS = (
    EMBEDDED_LICENSE_PUBLIC_KEYS
    if EMBEDDED_LICENSE_PUBLIC_KEYS is not None
    else os.environ.get("AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS", "")
).strip()
LICENSE_REQUEST_TIMEOUT = float(os.environ.get("AI_VOICE_STUDIO_LICENSE_TIMEOUT", "12"))
LICENSE_REFRESH_SECONDS = max(
    300, int(os.environ.get("AI_VOICE_STUDIO_LICENSE_REFRESH_SECONDS", "3600"))
)

# --------------------------------------------------------------------------- #
# Model catalog trust configuration
# --------------------------------------------------------------------------- #
# The signed model catalog is a separate trust domain from licensing: rotating
# the model key never affects license validation, and a compromised model key
# cannot mint licenses. Ed25519 public keys only — the private half never ships.
MODEL_MANIFEST_URL = os.environ.get("AI_VOICE_STUDIO_MODEL_MANIFEST_URL", "").strip()
MODEL_PUBLIC_KEYS = (
    os.environ.get("AI_VOICE_STUDIO_MODEL_PUBLIC_KEYS", "") or ""
).strip()
# Optional mirror (Airgapped/offline deployments, corporate proxies). Must be a
# plain host; the https scheme is enforced by the catalog validator.
MODEL_MIRROR_BASE = os.environ.get("AI_VOICE_STUDIO_MODEL_MIRROR", "").strip().rstrip("/")
MODEL_CATALOG_TIMEOUT = float(
    os.environ.get("AI_VOICE_STUDIO_MODEL_CATALOG_TIMEOUT", "20")
)
MODEL_DOWNLOAD_TIMEOUT = float(
    os.environ.get("AI_VOICE_STUDIO_MODEL_DOWNLOAD_TIMEOUT", "60")
)
# Refuse to start a download that would not fit on the target volume. Slack is
# added on top of the declared size for archive expansion and the temp file.
MODEL_DISK_HEADROOM_BYTES = max(
    64 * 1024 * 1024,
    int(os.environ.get("AI_VOICE_STUDIO_MODEL_DISK_HEADROOM", str(512 * 1024 * 1024))),
)

# Project root is two levels up from this file (app/config.py -> project root)
BASE_DIR = Path(__file__).resolve().parent.parent

# Every piece of runtime data (settings, history, generated audio and all
# downloaded models) lives in ONE folder outside the project. Override it with
# the AI_VOICE_STUDIO_DATA environment variable if you move the folder.
DATA_ROOT = Path(
    os.environ.get("AI_VOICE_STUDIO_DATA", str(Path.home() / "AI Voice Studio Data"))
).expanduser()
DATA_DIR = DATA_ROOT
OUTPUT_DIR = DATA_ROOT / "output"
MODELS_DIR = DATA_ROOT / "models"

# The signed model catalog bundled inside the build. Only a development
# convenience: a release ships an empty catalog plus a configured manifest URL.
BUNDLED_MODEL_CATALOG = Path(__file__).resolve().parent / "resources" / "catalog.json"

# Force every third-party library to keep its model cache inside our folder, so
# nothing is left behind in %USERPROFILE%\.local, \.cache or \.config.
LOCAL_HF_HOME = MODELS_DIR / "huggingface"

# Development fallback: builds used to keep their Hugging Face cache beside the
# source tree. A machine that downloaded a model that way still has a usable
# copy, so the model screen adopts it instead of re-fetching several gigabytes.
LEGACY_PROJECT_HF_HOME = Path(__file__).resolve().parents[1] / "models" / "huggingface"

_CACHE_ENV = {
    "TTS_HOME": MODELS_DIR / "tts",
    "HF_HOME": LOCAL_HF_HOME,
    "XDG_DATA_HOME": MODELS_DIR / "xdg-data",
    "XDG_CACHE_HOME": MODELS_DIR / "xdg-cache",
    "XDG_CONFIG_HOME": MODELS_DIR / "xdg-config",
}
for _key, _path in _CACHE_ENV.items():
    os.environ.setdefault(_key, str(_path))

# Output sub-folders, all created eagerly at startup
OUTPUT_SUBDIRS = {
    "tts": OUTPUT_DIR / "tts",
    "recordings": OUTPUT_DIR / "recordings",
    "transcripts": OUTPUT_DIR / "transcripts",
    "voices": OUTPUT_DIR / "voices",
    "clones": OUTPUT_DIR / "clones",
}

DATA_FILES = {
    "settings": DATA_DIR / "settings.json",
    "history": DATA_DIR / "history.json",
}

XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

# Optional Qwen3-TTS voice-cloning model (CUDA GPU recommended, ~4.5 GB).
QWEN_MODEL_NAME = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"

# Qwen3-TTS runs on an NVIDIA CUDA GPU when one is available, otherwise it
# falls back to the (slow) CPU. Set to True to force GPU-only mode (it then
# refuses to load on CPU and reports a clear GPU-required error instead).
QWEN_GPU_ONLY = False

# The Qwen3-TTS Base model is a voice-cloning model: main TTS synthesises with
# a reference voice. The default reference (a neutral sample generated once
# from the offline system voice) lives here; users can record their own.
QWEN_VOICES_DIR = MODELS_DIR / "qwen-voices"
QWEN_DEFAULT_REF_VOICE = QWEN_VOICES_DIR / "qwen-default-voice.wav"

# Local (offline) TTS voice models for the optional Piper backend.
PIPER_VOICE_DIR = MODELS_DIR / "piper-voices"
# Written once the user has been offered the one-time "download every voice"
# prefetch, so the app asks a single time instead of on every launch. The Voices
# screen keeps a button for it either way.
PIPER_PREFETCH_MARKER = DATA_ROOT / ".piper-prefetch-offered"

# Local (offline) speech-recognition models.
VOSK_MODEL_DIR = MODELS_DIR / "vosk-models"

# Downloaded model archives are staged here before their SHA-256 is verified and
# they are atomically moved into place. Staging never overlaps MODELS_DIR
# sub-folders, so a partial or unverified download can never be mistaken for an
# installed model. Cached here so an interrupted download can be resumed.
MODEL_STAGING_DIR = DATA_ROOT / "model-staging"
MODEL_CATALOG_CACHE = DATA_ROOT / "model-catalog.json"

# Every model sub-folder created eagerly under MODELS_DIR at startup.
MODEL_SUBDIRS = {
    "vosk": VOSK_MODEL_DIR,
    "piper": PIPER_VOICE_DIR,
    "qwen-voices": QWEN_VOICES_DIR,
    "tts": MODELS_DIR / "tts",
    "huggingface": LOCAL_HF_HOME,
    "xdg-data": MODELS_DIR / "xdg-data",
    "xdg-cache": MODELS_DIR / "xdg-cache",
    "xdg-config": MODELS_DIR / "xdg-config",
}

DEFAULT_SETTINGS: dict = {
    "appearance": "dark",
    "tts": {
        "language": "en",
        "voice": "",
        "speed": 1.0,
        "pitch": 0,
        # "qwen3"  = Qwen3-TTS Base (local, offline; model in models\huggingface)
        # "system" = Windows SAPI5 voices via pyttsx3 (offline, OS languages only)
        # "piper"  = optional Piper neural voices (offline, downloaded voices)
        "backend": "qwen3",
        # Optional: path to a recorded voice profile used as the Qwen3-TTS
        # reference voice for the main Text-to-Speech engine. Empty = auto.
        "qwen_reference": "",
    },
    "stt": {
        # Offline only: "vosk" (default), optional "whisper" when installed.
        "engine": "vosk",
        "language": "en-US",
    },
    "recorder": {
        "device": None,
        "samplerate": 44100,
        "channels": 1,
    },
    "output": {
        "folder": str(OUTPUT_DIR),
        "format": "wav",
    },
    "clone": {
        # Heavy/experimental: requires torch + ~8 GB RAM.
        # "engine": "xtts" (Coqui XTTS-v2) or "qwen" (Qwen3-TTS Base, CUDA GPU).
        "enabled": False,
        "engine": "xtts",
        "fallback_voice": "",
        "consent": False,
    },
}

# A small human-friendly language map used when a voice's locale is missing.
LOCALE_NAMES = {
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "az": "Azerbaijani",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "bs": "Bosnian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "eo": "Esperanto",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "ga": "Irish",
    "gl": "Galician",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "is": "Icelandic",
    "it": "Italian",
    "ja": "Japanese",
    "jv": "Javanese",
    "kk": "Kazakh",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "ky": "Kyrgyz",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "my": "Burmese",
    "nb": "Norwegian",
    "ne": "Nepali",
    "nl": "Dutch",
    "pb": "Punjabi",
    "ps": "Pashto",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "sq": "Albanian",
    "sr": "Serbian",
    "su": "Sundanese",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "zt": "Chinese (Traditional)",
    "zu": "Zulu",
}


# Google Web Speech widely-supported BCP-47 language codes (~125 languages).
STT_LANGUAGES = [
    "af-ZA",
    "am-ET",
    "ar-AE",
    "ar-BH",
    "ar-DZ",
    "ar-EG",
    "ar-IQ",
    "ar-JO",
    "ar-KW",
    "ar-LB",
    "ar-MA",
    "ar-OM",
    "ar-PS",
    "ar-QA",
    "ar-SA",
    "ar-TN",
    "ar-YE",
    "as-IN",
    "az-AZ",
    "bg-BG",
    "bn-BD",
    "bn-IN",
    "bs-BA",
    "ca-ES",
    "cs-CZ",
    "cy-GB",
    "da-DK",
    "de-AT",
    "de-CH",
    "de-DE",
    "el-GR",
    "en-AU",
    "en-CA",
    "en-GB",
    "en-GH",
    "en-IE",
    "en-IN",
    "en-KE",
    "en-NG",
    "en-NZ",
    "en-PH",
    "en-PK",
    "en-TZ",
    "en-US",
    "en-ZA",
    "es-AR",
    "es-BO",
    "es-CL",
    "es-CO",
    "es-CR",
    "es-DO",
    "es-EC",
    "es-ES",
    "es-GT",
    "es-HN",
    "es-MX",
    "es-NI",
    "es-PA",
    "es-PE",
    "es-PR",
    "es-PY",
    "es-SV",
    "es-US",
    "es-UY",
    "es-VE",
    "et-EE",
    "eu-ES",
    "fa-IR",
    "fi-FI",
    "fil-PH",
    "fr-BE",
    "fr-CA",
    "fr-CH",
    "fr-FR",
    "gl-ES",
    "gu-IN",
    "he-IL",
    "hi-IN",
    "hr-HR",
    "hu-HU",
    "hy-AM",
    "id-ID",
    "is-IS",
    "it-CH",
    "it-IT",
    "ja-JP",
    "jv-ID",
    "ka-GE",
    "kk-KZ",
    "km-KH",
    "kn-IN",
    "ko-KR",
    "lo-LA",
    "lt-LT",
    "lv-LV",
    "mk-MK",
    "ml-IN",
    "mn-MN",
    "mr-IN",
    "ms-MY",
    "my-MM",
    "ne-NP",
    "nl-BE",
    "nl-NL",
    "no-NO",
    "pa-Guru-IN",
    "pl-PL",
    "pt-BR",
    "pt-PT",
    "ro-RO",
    "ru-RU",
    "si-LK",
    "sk-SK",
    "sl-SI",
    "so-SO",
    "sq-AL",
    "sr-RS",
    "su-ID",
    "sv-SE",
    "sw-KE",
    "sw-TZ",
    "ta-IN",
    "ta-LK",
    "te-IN",
    "th-TH",
    "tr-TR",
    "uk-UA",
    "ur-IN",
    "ur-PK",
    "uz-UZ",
    "vi-VN",
    "yue-Hant-HK",
    "zh-CN",
    "zh-TW",
    "zu-ZA",
]


def locale_language_code(locale: str) -> str:
    """Return the primary language subtag for a locale (e.g. en-US -> en)."""
    return (locale or "").split("-")[0].strip().lower()


def language_display_name(locale: str) -> str:
    """Human-readable language name for a locale code."""
    code = locale_language_code(locale)
    return LOCALE_NAMES.get(code, code.upper() if code else locale)


def settings_path() -> Path:
    return DATA_FILES["settings"]


def history_path() -> Path:
    return DATA_FILES["history"]
