"""Application-wide configuration, default paths and constants."""
from __future__ import annotations

from pathlib import Path

APP_NAME = "AI Voice Studio"
APP_VERSION = "1.0.0"

# Project root is two levels up from this file (app/config.py -> project root)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

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

DEFAULT_SETTINGS: dict = {
    "appearance": "dark",
    "tts": {
        "language": "en",
        "voice": "",
        "speed": 1.0,
        "pitch": 0,
    },
    "stt": {
        "engine": "google",
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
        "fallback_voice": "en-US-ChristopherNeural",
        "consent": False,
    },
}

# A small human-friendly language map used when edge-tts metadata is missing.
LOCALE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "az": "Azerbaijani",
    "bg": "Bulgarian", "bn": "Bengali", "bs": "Bosnian", "ca": "Catalan",
    "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fil": "Filipino", "fr": "French",
    "gu": "Gujarati", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian",
    "hu": "Hungarian", "id": "Indonesian", "is": "Icelandic", "it": "Italian",
    "ja": "Japanese", "jv": "Javanese", "kk": "Kazakh", "km": "Khmer",
    "kn": "Kannada", "ko": "Korean", "lo": "Lao", "lt": "Lithuanian",
    "lv": "Latvian", "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian",
    "mr": "Marathi", "ms": "Malay", "mt": "Maltese", "my": "Burmese",
    "nb": "Norwegian", "ne": "Nepali", "nl": "Dutch", "ps": "Pashto",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "so": "Somali",
    "sq": "Albanian", "sr": "Serbian", "su": "Sundanese", "sv": "Swedish",
    "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek",
    "vi": "Vietnamese", "zh": "Chinese", "zu": "Zulu",
}


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
