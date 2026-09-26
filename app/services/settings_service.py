"""Persistent user settings stored as JSON, validated against defaults."""
from __future__ import annotations

import json
import threading

from app import config
from app.services import file_service


def _deep_merge(defaults: dict, user: dict) -> dict:
    merged = dict(defaults)
    for key, value in (user or {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class SettingsService:
    """Load/store application settings with a simple in-memory cache."""

    def __init__(self) -> None:
        self._lock = threading.RLock()  # RLock so save() can be called from load()/set()
        self._data = dict(config.DEFAULT_SETTINGS)
        self._path = config.settings_path()

    # -- lifecycle -----------------------------------------------------------
    def load(self) -> SettingsService:
        with self._lock:
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    self._data = _deep_merge(dict(config.DEFAULT_SETTINGS), raw)
                except (json.JSONDecodeError, OSError):
                    self._data = dict(config.DEFAULT_SETTINGS)
            else:
                self._data = dict(config.DEFAULT_SETTINGS)
                self.save()
        return self

    def save(self) -> None:
        with self._lock:
            file_service.ensure_dirs()
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # -- access --------------------------------------------------------------
    def get(self, section: str, key: str, default=None):
        section_data = self._data.get(section, {})
        if isinstance(section_data, dict):
            return section_data.get(key, default)
        return default

    def set(self, section: str, key: str, value) -> None:
        with self._lock:
            self._data.setdefault(section, {})[key] = value
        self.save()

    def as_dict(self) -> dict:
        return dict(self._data)

    def reset(self) -> None:
        with self._lock:
            self._data = dict(config.DEFAULT_SETTINGS)
        self.save()
